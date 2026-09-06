"""治理精度回归（v2，2026-09-06 LongMemEval-S 真实误杀案例固化）。

锚点来自 data/aml-materials 样本审计：
- 75499fd8：旧=问狗项圈/提及 Golden Retriever；新=同主体聊 Kong 玩具（含
  "k**now**"——v1 把 schema 'now' 子串命中当更新信号）→ 必须 kept
- 545bd2b5：旧=陈述日均 2 小时屏幕时间；新="do you think ... set a time
  limit"（含 don't 但为询问/打算）→ 必须 kept
同时锁死既有更新语义（e2e 三连）不回退：currently/no longer/now 真更新仍取代。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from cortexa.config import Settings
from cortexa.llm import LLMClient
from cortexa.models import AddRequest, AmlMessage
from cortexa.modules.pipeline import run_add
from cortexa.storage import SQLiteMemoryStore


def _status_after_pair(old_text: str, new_text: str) -> str:
    """old 先入库，new 后入库，返回 old 记忆的最终 status。"""
    with tempfile.TemporaryDirectory(prefix="govprec-") as td:
        settings = Settings(data_dir=Path(td), embedding_provider="hash",
                            scene="aml", journal_mode="off",
                            governance_enabled=True, _env_file=None)
        store = SQLiteMemoryStore(str(settings.data_dir / "cortexa.db"),
                                  embedding_dim=settings.embedding_dim)
        try:
            llm = LLMClient(settings)
            for content in (old_text, new_text):
                req = AddRequest(request_id="r", scope="s", session_id="s",
                                 messages=[AmlMessage(role="user", content=content)])
                run_add(store, llm, settings, req, journal=None)
            row = store.connection.execute(
                "SELECT status FROM memories WHERE scope=? AND content=?",
                ("s", old_text)).fetchone()
            return row["status"] if row else "MISSING"
        finally:
            store.close()


CASES_KEEP = [
    # R1 75499fd8：词边界（now 不再命中 know）+ 不同属性（项圈咨询 vs 玩具闲聊）
    ("75499fd8-dog-collar-vs-kong-toy",
     "I'm thinking of getting Max a new collar with a nice name tag. Do you have any recommendations for a good collar brand or type that would suit a Golden Retriever like Max?",
     "I think Max would love the Kong Toy. I already know he loves peanut butter, and it would be great to stuff the Kong with peanut butter-filled Kongs as a special treat."),
    # R2 545bd2b5：don't 命中的是询问/打算，不是状态更新
    ("545bd2b5-screen-time-vs-time-limit-idea",
     "I've been averaging around 2 hours of screen time on Instagram per day for the past two weeks, which is way too much. Do you think setting a time limit of 30 minutes per day would be a good starting point for me?",
     "I like the idea of designating specific check-in times. But do you think it's possible to also set a time limit for social media apps like Instagram, so I don't end up spending too much time on them?"),
    # N1 防过度既有锚：补充性陈述不误杀（v1 语义保持）
    ("alice-pet-vs-drink",
     "Alice prefers tea over coffee",
     "Alice adopted a dog named Rob"),
]

CASES_SUPERSEDE = [
    # P1-P4 既有 e2e 更新语义不得回退
    ("alice-currently", "Alice prefers tea over coffee", "Alice currently prefers coffee"),
    ("alice-no-longer", "Alice prefers tea over coffee", "Alice no longer prefers tea"),
    ("bob-now-teams", "Bob prefers Slack over Teams for chat", "Bob now prefers Teams over Slack"),
    ("bob-no-longer-pnpm", "Bob uses pnpm for builds", "Bob no longer uses pnpm"),
]


def test_real_cases_kept() -> None:
    for name, old, new in CASES_KEEP:
        status = _status_after_pair(old, new)
        assert status == "ACTIVE", (name, status)


def test_update_semantics_still_supersede() -> None:
    for name, old, new in CASES_SUPERSEDE:
        status = _status_after_pair(old, new)
        assert status == "SUPERSEDED", (name, status)


def test_soft_supersede_keeps_old_active_with_decay() -> None:
    """B2（T1.4）：soft 模式下旧值不硬隐藏，retrieval_weight 降为 30% 仍 ACTIVE。"""
    with tempfile.TemporaryDirectory(prefix="govsoft-") as td:
        settings = Settings(data_dir=Path(td), embedding_provider="hash",
                            scene="aml", journal_mode="off",
                            governance_enabled=True,
                            governance_soft_enabled=True, _env_file=None)
        store = SQLiteMemoryStore(str(settings.data_dir / "cortexa.db"),
                                  embedding_dim=settings.embedding_dim)
        try:
            llm = LLMClient(settings)
            for content in ("Alice prefers tea over coffee",
                            "Alice currently prefers coffee"):
                req = AddRequest(request_id="r", scope="s", session_id="s",
                                 messages=[AmlMessage(role="user", content=content)])
                run_add(store, llm, settings, req, journal=None)
            row = store.connection.execute(
                "SELECT status, retrieval_weight FROM memories "
                "WHERE scope=? AND content=?",
                ("s", "Alice prefers tea over coffee")).fetchone()
            assert row is not None
            assert row["status"] == "ACTIVE", row["status"]  # 软：仍在库可返
            assert row["retrieval_weight"] <= 0.35, row["retrieval_weight"]
            # 硬对照（默认）仍在既有用例 CASES_SUPERSEDE 覆盖
        finally:
            store.close()


def test_signal_boundary_and_hypothetical_guards() -> None:
    """v2 内部判据抽查：词边界使 know 内的 now 不再成信号。"""
    from cortexa.domain.governance import GovernanceEngine
    from cortexa.schema import get_schema

    gov = GovernanceEngine(None, get_schema("aml"))  # type: ignore[arg-type]
    assert gov._signal("I already know he loves peanut butter") is None
    assert gov._signal("Alice currently prefers coffee") == ("schema", "currently")
    assert gov._signal("Bob no longer uses pnpm") == ("negate", "no longer")
    assert gov._hypothetical("do you think it is possible to set a limit?") is True
