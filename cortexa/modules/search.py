"""检索管线（W2）：多通道召回 -> RRF 融合 -> RULE 加成 -> 保留度调制 -> precision-first 截断。

通道：FTS5 精确（维度 A）+ 向量语义 + 编号精确（章节锚点，可经
settings.chapter_channel 关闭）+ 实体特征访问（维度 B 前哨，默认关闭，
实际使用 A/B 未证明价值前不占用默认路径，见 Settings.entity_channel 注释）。
官方契约：results 按相关性降序、空数组合法、尊重 top_k（冒烟 Top K 90）。
precision-first 依据：官方 pipeline.py 对列表题多答判 WRONG——宁可少给不可硬凑。
缺席闸（布隆哲学：把"不在"做成廉价确定答案）：空 scope 免嵌入/检索直接返回空。
vector_min_cos 在存储层已剪枝语义通道；本层只做融合与截断，不引入新的召回源。
W3 接线：检索命中即强化（测试效应）——最终返回的记忆 FSRS 巩固 +1；
检索事件写入 TaskJournal（模式由 Settings.journal_mode 决定）。
"""

from __future__ import annotations

import re
import time

from ..config import Settings
from ..journal import TaskJournal
from ..llm import LLMClient
from ..models import MemoryInstance, SearchResult, to_search_result
from ..storage import MemoryStore

# 编号精确通道强信号（2026-09-06 真实使用发现）：显式章号查询（"第107章讲了什么"）
# 若只按 RRF(k=60) 融合，章节命中（1/(60+r)≈0.016）会被弱 FTS 词重叠淹没——
# 实测 top3 漂到无关章节。章号是强意图信号，给该通道成员 ×CHAPTER_RRF_BOOST。
CHAPTER_RRF_BOOST = 6.0
# 语义软通道加成（真实使用写前 recall 用；参赛红线路径不开）
SEM_CHANNEL_BOOST = 3.0


def _retention_factor(mem: MemoryInstance, settings: Settings) -> float:
    """R(t)×retrieval_weight 融合调制因子（w3_fsrs_wire）。

    factor = 1 + w * (R_eff - 1)，w = settings.retrieval_fusion_weight。
    - RULE / no_decay（维度 F 红线）：强制中性 factor=1.0，规则永不因衰减下调；
    - 从未成功检索过（last_review_at is None）：R_eff 视为 1.0，factor=1.0
      ——保证首次评测（无任何复习）排序与调制前完全一致，不引入退化；
    - 已老化记忆：按遗忘曲线 R(t)<1 温和下调（下限 1-w，绝不归零/放大）。
    只作用于最终排序，不改变召回集合（factor>0，precision-first 截断不变）。
    """
    if mem.mem_type.value == "RULE" or mem.no_decay:
        return 1.0
    w = settings.retrieval_fusion_weight
    if w <= 0 or mem.fsrs.last_review_at is None:
        return 1.0
    # retrieval_weight（衰减遗忘的作用旋钮，默认 1.0）与 R(t) 同源进融合：
    # 遗忘引擎（P4）落地后下调 retrieval_weight 即直接影响排序，无需再改本处。
    r = min(max(mem.fsrs.retrievability() * max(mem.retrieval_weight, 0.0), 0.0), 1.0)
    return max(1.0 - w, 1.0 + w * (r - 1.0))


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_numeral(text: str) -> int | None:
    """中文数字→int（支持到千位：一百零七/三十七/两百零一）；非法返回 None。"""
    if not text:
        return None
    total, section, num = 0, 0, 0
    for ch in text:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        elif ch == "千":
            section += (num or 1) * 1000
            num = 0
        else:
            return None
    total = section + num
    return total if 1 <= total <= 9999 else None


def extract_chapter_nos(query: str) -> list[int]:
    """从查询提取章节号（编号精确通道的入口，零幻觉：只认显式章节模式）。

    支持：第107章 / 第 107 章 / 第三十七章 / Ch 40 / ch40。去重保序。
    """
    nos: list[int] = []
    for m in re.finditer(r"第\s*([0-9]{1,4})\s*章", query):
        nos.append(int(m.group(1)))
    for m in re.finditer(r"第\s*([零一二两三四五六七八九十百千]+)\s*章", query):
        n = _cn_numeral(m.group(1))
        if n is not None:
            nos.append(n)
    for m in re.finditer(r"(?<![A-Za-z0-9])[Cc][Hh][.\s]*([0-9]{1,4})(?![0-9])", query):
        nos.append(int(m.group(1)))
    seen: set[int] = set()
    return [n for n in nos if not (n in seen or seen.add(n))]


def search_memory(
    store: MemoryStore,
    llm: LLMClient,
    settings: Settings,
    *,
    query: str,
    scope: str,
    top_k: int,
    journal: TaskJournal | None = None,
    options: list[str] | None = None,
) -> list[SearchResult]:
    """Search 契约的实现核心。返回官方四字段降序数组。

    融合公式（业务文档 8.x 三流融合思路的 MVP 两流版）：
        score = Σ 1/(rrf_k + rank)   （FTS 与向量各自排名后相加）
    RULE 记忆融合分乘 rule_boost——只影响排序不扩大召回集（precision-first 不变）。
    """
    started = time.perf_counter()
    k = max(1, min(top_k, settings.max_top_k))

    # G1（2026-09-06，InvMem 查询构造）：选择题把 question+options 拼接为统一
    # 检索查询——选项里的实体/候选事实也参与召回；原文案/限流由官方 Answer 侧管。
    # Q2a-D（2026-09-06，外部复核 v3 §5.3，默认关）：双通道档改为题干独立作主查询
    # + 选项低权副通道（见后），对照 G1 拼接档做 A/B。journal 永远记录原始 query。
    dual_options = bool(options) and settings.options_dual_channel_enabled
    eff_query = query if dual_options else (
        (query + " " + " ".join(options)).strip() if options else query)

    def _empty_journal() -> list[SearchResult]:
        if journal is not None:
            journal.record(
                "search", scope=scope, query=query, top_k=k, hits=[],
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        return []

    # 缺席闸：scope 无活跃记忆 = 确定性"不在"，O(1) 证明，跳过嵌入与全部检索开销。
    if store.count(scope) == 0:
        return _empty_journal()

    fused: dict[str, float] = {}
    hits: dict[str, tuple[MemoryInstance, float]] = {}
    # 候选池通道来源（P2-5b，2026-09-06）：每个进入融合的 id 记录贡献通道
    # （fts/vec/sem/chapter/entity），journal hits 随结果落库——"候选从哪来"可审计
    srcs: dict[str, set[str]] = {}

    def _merge(ranked: list[tuple[MemoryInstance, float]], channel: str,
               weight: float = 1.0) -> None:
        for rank, (mem, _raw) in enumerate(ranked, start=1):
            # E1（2026-09-06）：RRF 通道权重（InvMem 词法 0.5/dense 1.0 对照；
            # 默认等权=1.0 行为不变）。只作用于融合贡献，不改召回集。
            fused[mem.id] = fused.get(mem.id, 0.0) + weight / (settings.rrf_k + rank)
            hits[mem.id] = (mem, fused[mem.id])
            srcs.setdefault(mem.id, set()).add(channel)

    _merge(store.fts_search(eff_query, scope, limit=k * 3), "fts",
           settings.rrf_lexical_weight)
    _provider = settings.embedding_provider
    # G1 语义软通道（2026-09-06）：openai provider + semantic_channel + 存在索引才走；
    # 语义成员融合分 ×SEM_CHANNEL_BOOST（软补充不主导）；无索引/维度不符自动回退。
    _use_sem = (
        _provider == "openai" and settings.semantic_channel
        and store.sem_count(scope) > 0
    )
    if _use_sem:
        # 语义软通道（独立表 1024）+ 并行补跑 hash 向量通道（512 存量矩阵）——
        # hybrid A/B 实证：只留语义会让「主线」类总结题回退（hash 通道丢了）；
        # 双通道并存由 RRF 天然降权，各自补位。
        # 语义模式小改（2026-09-06，单点可回退）：候选不足是弱题漏检主因——
        # 仅在语义通道开启时把 FTS 候选加宽到 80 并入融合；hash 默认路径零改动。
        _merge(store.fts_search(eff_query, scope, limit=80), "fts",
               settings.rrf_lexical_weight)
        sem_mems = store.sem_search(
            llm.embed([eff_query])[0], scope, limit=settings.semantic_candidates,
            min_cos=settings.vector_min_cos,
        )
        _merge(sem_mems, "sem")
        for sem_mem, _raw in sem_mems:
            fused[sem_mem.id] = fused.get(sem_mem.id, 0.0) * SEM_CHANNEL_BOOST
            hits[sem_mem.id] = (sem_mem, fused[sem_mem.id])
        hdim = store.vector_dim(scope)
        if hdim is not None:
            from ..llm import HashEmbedder
            _merge(
                store.vector_search(
                    HashEmbedder(dim=hdim).embed(eff_query), scope, limit=k * 3,
                    min_cos=settings.vector_min_cos_hash,
                ), "vec", settings.rrf_vector_weight,
            )
    elif _provider == "hash":
        # hash 向量与语义索引分库共存；hash provider 永不触碰 sem 表
        _merge(
            store.vector_search(
                llm.embed([eff_query])[0], scope, limit=k * 3,
                min_cos=settings.vector_min_cos_hash,
            ), "vec", settings.rrf_vector_weight,
        )
    # provider=openai 且（semantic_channel 关 或 无索引）：FTS/chapter/entity 兜底，
    # 不发 hash 向量（防 512/1024 维数错乱）
    if settings.entity_channel:
        _merge(store.entity_search(eff_query, scope, limit=k * 3), "entity")
    if settings.chapter_channel:
        chapter_nos = extract_chapter_nos(eff_query)
        if chapter_nos:
            ch_mems = store.chapter_search(chapter_nos, scope, limit=k * 3)
            _merge(ch_mems, "chapter")
            for ch_mem, _raw in ch_mems:
                # 注意：_merge 已把 fused 值快照进 hits——boost 后必须同步回写快照，
                # 否则 scored 阶段读到的是未 boost 的旧值（2026-09-06 实测踩坑）。
                fused[ch_mem.id] = fused.get(ch_mem.id, 0.0) * CHAPTER_RRF_BOOST
                hits[ch_mem.id] = (ch_mem, fused[ch_mem.id])

    # T1.1 J1（默认关）：确定性查询规划——entities 字段走 entity 特征合并通道
    if settings.query_plan_enabled and len(hits) < k:
        from .query_plan import parse_query_plan
        _plan = parse_query_plan(query)
        if _plan["entities"]:
            _merge(
                store.entity_search(" ".join(_plan["entities"]), scope, limit=k * 3),
                "plan",
            )

    # Q2a-D（默认关）：选项低权副通道——题干主查询之上，选项文本单独召回候选并
    # 以 options_channel_weight 降权进融合（防选项实体淹没题干；对照 G1 拼接）。
    if dual_options:
        _opt_q = " ".join(options)
        _merge(store.fts_search(_opt_q, scope, limit=k * 3), "opts",
               settings.rrf_lexical_weight * settings.options_channel_weight)
        if settings.entity_channel:
            _merge(store.entity_search(_opt_q, scope, limit=k * 3), "opts",
                   settings.options_channel_weight)

    # T1.3 H1（默认关）：会话级 RRF 第二项 r2（ReFind cluster boost）
    bonus: dict[str, float] = {}
    if settings.session_rrf_enabled and len(hits) > 1:
        from .session_rrf import session_rrf_bonus
        bonus = session_rrf_bonus(
            hits, k=settings.rrf_k, weight=settings.session_rrf_weight
        )

    scored: list[tuple[MemoryInstance, float]] = []
    for mem, score in hits.values():
        if mem.mem_type.value == "RULE":
            score *= settings.rule_boost  # 维度 F：规则稳定可见
        score *= _retention_factor(mem, settings)  # 保留度调制（首过中性，零退化）
        if settings.time_decay_enabled and not (mem.no_decay):
            # R1 年龄衰减（默认关）：RULE 之外按事件时间降权（旧→低）
            from .decay import age_factor
            _ts = mem.event_ts_ms if mem.event_ts_ms is not None else mem.created_ts_ms
            score *= age_factor(_ts, half_days=settings.time_decay_half_days)
        if bonus:
            score += bonus.get(mem.id, 0.0)
        scored.append((mem, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    if settings.temporal_filter_enabled:
        # R4 时间过滤（默认关）：查询年份线索按 event_ts 年份过滤（RULE 豁免在模块内）
        from .temporal_filter import apply_year_filter, year_cues
        _years = year_cues(eff_query)
        if _years:
            scored = apply_year_filter(scored, _years, set())
    if settings.time_anchor_enabled:
        # F2 检索侧（默认关，temporal_conflict_eval_v1）：相对时间窗过滤——query 带
        # last month/上个月/N months ago/last year/昨天 等线索时，按日历窗口
        # （month/year 整单位、day/week 自然日）过滤候选。ref = scope 内最新消息
        # 时间（消息陈述即事件时刻的语料语义）；无线索 = 原样返回。
        from .time_anchor import apply_relative_window
        try:
            _ref_ms = store.connection.execute(
                "SELECT MAX(COALESCE(event_ts_ms, created_ts_ms)) "
                "FROM memories WHERE scope = ?", (scope,),
            ).fetchone()[0]
        except Exception:  # noqa: BLE001 —— 查询失败按无过滤处理（观察面）
            _ref_ms = None
        if _ref_ms:
            scored = apply_relative_window(scored, query, _ref_ms)
    if settings.recency_arbitration_enabled:
        # recency-arbitrated retrieval（默认关）：冲突分量内显式重排为最新在前
        # （v4 分量级：无冲突边成员位置不动）。keep_prev>0=C4（保留最新+最近
        # predecessor）。RAR-002 意图门（默认关）：仅 CURRENT/LIST_CURRENT 查询
        # 仲裁；HISTORICAL/SEQUENCE/ENUM/DEFAULT 先走年份/相对窗过滤，不仲裁。
        from .query_intent import classify_intent
        from .recency_arbitration import arbitrate
        if settings.recency_arbitration_intent_gated:
            _intn = classify_intent(query)["kind"]
            if _intn == "LIST_CURRENT":
                scored = arbitrate(
                    scored, top_n=settings.recency_arbitration_top_n,
                    keep_prev=0,
                    list_mode=settings.recency_arbitration_list_suppress,
                )
            elif _intn == "CURRENT":
                scored = arbitrate(
                    scored, top_n=settings.recency_arbitration_top_n,
                    keep_prev=settings.recency_arbitration_keep_prev,
                )
            # 其它意图：不仲裁（时间件已在前面处理）
        else:
            scored = arbitrate(
                scored, top_n=settings.recency_arbitration_top_n,
                keep_prev=settings.recency_arbitration_keep_prev,
            )
    results = [to_search_result(mem, score) for mem, score in scored[:k]]

    # 检索命中即强化（测试效应）：只有真正返回的记忆才巩固，与日志开关无关
    store.touch_retrieval([r.id for r in results], scope)

    # RARE-lite：检索增强重编码——同题命中的记忆两两关联写回（确定性零 LLM）。
    # 门控 = novel_adapter_enabled（P2-5b，2026-09-06）：RARE 属小说适配器能力，
    # 只在适配器启用（真实小说流）时写记忆桥；参赛/通用路径（adapter 关）Search 保持纯读。
    if settings.rare_enabled and settings.novel_adapter_enabled and len(results) >= 2:
        from .novel_adapter import NovelAdapter
        try:
            adapter = NovelAdapter(store, llm, settings)
            adapter.rare_strengthen([r.id for r in results], scope, top_n=settings.rare_top_n)
        except Exception as _exc:  # noqa: BLE001 —— RARE 写回失败不阻断检索，但进观察面
            from ..enhance_stats import stats
            stats.inc("rare", err=_exc)

    # ---- 领域适配器 pre_return hook ----
    if settings.novel_adapter_enabled and settings.adapter_lifecycle_guard and results:
        from .novel_adapter import NovelAdapter
        adapter = NovelAdapter(store, llm, settings)
        results = adapter.guard_lifecycle(results, scope)

    if journal is not None:
        journal.record(
            "search",
            scope=scope,
            query=query,
            top_k=k,
            hits=[
                {"id": r.id, "score": round(r.score, 6),
                 "channels": sorted(srcs.get(r.id, ()))}
                for r in results
            ],
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    return results