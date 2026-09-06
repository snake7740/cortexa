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
) -> list[SearchResult]:
    """Search 契约的实现核心。返回官方四字段降序数组。

    融合公式（业务文档 8.x 三流融合思路的 MVP 两流版）：
        score = Σ 1/(rrf_k + rank)   （FTS 与向量各自排名后相加）
    RULE 记忆融合分乘 rule_boost——只影响排序不扩大召回集（precision-first 不变）。
    """
    started = time.perf_counter()
    k = max(1, min(top_k, settings.max_top_k))

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

    def _merge(ranked: list[tuple[MemoryInstance, float]]) -> None:
        for rank, (mem, _raw) in enumerate(ranked, start=1):
            fused[mem.id] = fused.get(mem.id, 0.0) + 1.0 / (settings.rrf_k + rank)
            hits[mem.id] = (mem, fused[mem.id])

    _merge(store.fts_search(query, scope, limit=k * 3))
    _merge(
        store.vector_search(
            llm.embed([query])[0], scope, limit=k * 3,
            # provider 自适应下限：hash 词袋余弦阈值更严（0.25），语义嵌入用 0.05
            min_cos=(settings.vector_min_cos_hash
                     if settings.embedding_provider == "hash"
                     else settings.vector_min_cos),
        )
    )
    if settings.entity_channel:
        _merge(store.entity_search(query, scope, limit=k * 3))
    if settings.chapter_channel:
        chapter_nos = extract_chapter_nos(query)
        if chapter_nos:
            _merge(store.chapter_search(chapter_nos, scope, limit=k * 3))

    scored: list[tuple[MemoryInstance, float]] = []
    for mem, score in hits.values():
        if mem.mem_type.value == "RULE":
            score *= settings.rule_boost  # 维度 F：规则稳定可见
        score *= _retention_factor(mem, settings)  # 保留度调制（首过中性，零退化）
        scored.append((mem, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    results = [to_search_result(mem, score) for mem, score in scored[:k]]

    # 检索命中即强化（测试效应）：只有真正返回的记忆才巩固，与日志开关无关
    store.touch_retrieval([r.id for r in results], scope)

    # RARE-lite：检索增强重编码——同题命中的记忆两两关联写回（确定性零 LLM）
    if settings.rare_enabled and len(results) >= 2:
        from .novel_adapter import NovelAdapter
        try:
            adapter = NovelAdapter(store, llm, settings)
            adapter.rare_strengthen([r.id for r in results], scope, top_n=settings.rare_top_n)
        except Exception:
            pass  # RARE 写回失败不阻断检索（增强非必需）

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
            hits=[{"id": r.id, "score": round(r.score, 6)} for r in results],
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    return results
