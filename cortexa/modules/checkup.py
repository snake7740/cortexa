"""查体编排（"学习委员"）：多维度主动自检，一次跑出健康报告。

业务定位：主流 AI 记忆被动等查询；本模块把散落的检查器（维度地板复验/
一致性审计/孤岛统计）+ 两个新检查器（衰减健康度/探针自测）收拢为
一张成绩单——长篇内容越多，例行查体越必要。

纪律边界：
- 只评不改：报告异常清单，不做自动修正（误修比漏报伤，修正属 P4 登记项）；
- 零 LLM：全部确定性检查，不占 Add/Search 延迟（显式调用/空闲期跑）；
- 复用口径：孤岛统计与 audit 均直调既有实现，不自造统计逻辑。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from ..config import Settings
from ..journal import TaskJournal
from ..models import FSRSState, MemoryType
from ..storage import MemoryStore, SQLiteMemoryStore
from .consistency import audit as run_audit
from .dimension_floor import assert_floor
from .search import search_memory
from ..llm import LLMClient


@dataclass
class CheckupReport:
    """查体成绩单：每科一节；as_dict() 供 CLI/落盘。"""

    scope: str
    active_memories: int
    floor_total: int = 0          # 地板复验：全量条数
    floor_violations: int = 0     # 写入时红线应恒 0，非 0 = 历史脏数据信号
    orphans: dict = field(default_factory=dict)   # journal.orphan_stats 原样
    decay: dict = field(default_factory=dict)     # 衰减健康度（新检查器）
    probes: dict = field(default_factory=dict)    # 探针自测（新检查器）
    audit: dict | None = None     # 一致性审计：未给 subjects/items 则跳过

    def as_dict(self) -> dict:
        return asdict(self)


def _decay_health(rows: list[tuple[str, int, str]]) -> dict:
    """衰减健康度：非衰减豁免记忆的复习状况分桶。

    rows: (fsrs_json, mem_type, no_decay)。未复习记忆 R()=0.0，直接统计会
    把"从未被运用"与"老化"混为一谈——分桶区分（仿生：主动运用的反面）。
    """
    never_reviewed = healthy = aged = 0
    for fsrs_json, mem_type, no_decay in rows:
        if mem_type == MemoryType.RULE.value or no_decay:
            continue  # 豁免：规则永远待命（维度 F 红线）
        fsrs = FSRSState(**json.loads(fsrs_json))
        if fsrs.last_review_at is None:
            never_reviewed += 1
        elif fsrs.retrievability() < 0.5:
            aged += 1
        else:
            healthy += 1
    return {
        "checked": never_reviewed + healthy + aged,
        "never_reviewed": never_reviewed,  # 从未被检索命中强化
        "healthy": healthy,                # 复习过且 R(t) >= 0.5
        "aged": aged,                      # 复习过但 R(t) < 0.5（老化）
    }


def _probe_self_test(
    store: MemoryStore,
    llm: LLMClient,
    settings: Settings,
    journal: TaskJournal,
    scope: str,
    probes: list[tuple[str, str]],
    top_k: int,
) -> dict:
    """探针自测：(查询, 期望子串) 列表，期望子串出现在 top_k 任一命中即过。

    口径是"相关记忆是否进入 top_k"的覆盖度，非答案精度——诚实标注，
    精度判断交给官方评测与人工核对。
    """
    details: list[dict] = []
    hits = 0
    for query, expect in probes:
        results = search_memory(
            store, llm, settings, query=query, scope=scope, top_k=top_k, journal=journal
        )
        ok = any(expect in r.memory for r in results)
        hits += int(ok)
        details.append({"query": query, "expect": expect, "hit": ok})
    return {
        "total": len(probes),
        "hits": hits,
        "rate": round(hits / len(probes), 3) if probes else 0.0,
        "details": details,
    }


def run_checkup(
    store: MemoryStore,
    journal: TaskJournal,
    settings: Settings,
    llm: LLMClient,
    scope: str,
    *,
    subjects: list[str] | None = None,
    items: list[str] | None = None,
    probes: list[tuple[str, str]] | None = None,
    top_k: int = 3,
) -> CheckupReport:
    """对 scope 做一次全维度查体（只读，不改任何记忆）。

    顺序纪律：衰减健康度在探针自测之前算——探针检索会经测试效应
    （touch_retrieval）重置被命中记忆的复习时间，先算才能反映真实状况。
    """
    with store.lock:
        rows = store.connection.execute(
            "SELECT id, fsrs_json, mem_type, no_decay FROM memories "
            "WHERE scope = ? AND status = 'ACTIVE'",
            (scope,),
        ).fetchall()

    report = CheckupReport(scope=scope, active_memories=len(rows))

    # 1) 地板复验：写入时红线的全量复查
    report.floor_total = len(rows)
    with store.lock:
        full_rows = store.connection.execute(
            "SELECT * FROM memories WHERE scope = ? AND status = 'ACTIVE'", (scope,)
        ).fetchall()
    for row in full_rows:
        try:
            assert_floor(SQLiteMemoryStore._row_to_mem(row))
        except Exception:
            report.floor_violations += 1

    # 2) 孤岛统计（复用 journal 既有口径）
    report.orphans = journal.orphan_stats(scope)

    # 3) 衰减健康度（新检查器）
    report.decay = _decay_health([(r["fsrs_json"], r["mem_type"], r["no_decay"]) for r in rows])

    # 4) 探针自测（新检查器，放最后：其副作用是测试效应强化命中记忆）
    if probes:
        report.probes = _probe_self_test(
            store, llm, settings, journal, scope, probes, top_k
        )

    # 5) 一致性审计（调用方给名单才跑——系统不假装能自动识别人名）
    if subjects or items:
        report.audit = run_audit(
            store.connection,
            scope,
            subjects=subjects or [],
            items=items or [],
        ).as_dict()

    return report
