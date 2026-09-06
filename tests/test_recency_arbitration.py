"""recency-arbitrated retrieval 测试（纯函数 + 端到端 dated fixture）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from cortexa.config import Settings
from cortexa.llm import LLMClient
from cortexa.models import AddRequest, AmlMessage, MemoryInstance
from cortexa.modules.pipeline import run_add
from cortexa.modules.recency_arbitration import arbitrate
from cortexa.modules.search import search_memory
from cortexa.storage import SQLiteMemoryStore


def _mem(mid, subject, ts, content, attr=None):
    ents = [subject] + ([attr] if attr else [])
    return MemoryInstance(id=mid, scope="s", entities=ents, content=content,
                         event_ts_ms=ts, created_ts_ms=ts)


def test_rar_bonus_newest_first_in_conflict_cluster() -> None:
    old = _mem("a", "Team Nova", 1_646_000_000_000, "Team Nova selected Vendor A in 2022", "Vendor A")
    new = _mem("b", "Team Nova", 1_676_000_000_000, "Team Nova selected Vendor B in 2024", "Vendor B")
    other = _mem("c", "Alice", 1_600_000_000_000, "Alice prefers tea over coffee")
    out = arbitrate([(old, 1.0), (new, 1.0), (other, 1.0)], top_n=50)
    order = [m.id for m, _s in out]
    assert order.index("b") < order.index("a")  # 最新在前
    assert "c" in order  # 不同 subject 原样保留


def test_rar_no_conflict_no_bonus() -> None:
    m1 = _mem("a", "Team Nova", 1_646_000_000_000, "Team Nova hired Alice in 2022", "Alice")
    m2 = _mem("b", "Team Nova", 1_676_000_000_000, "Team Nova bought a printer in 2024", "Printer")
    out = arbitrate([(m1, 1.0), (m2, 1.0)], top_n=50)
    assert [m.id for m, _s in out] == ["a", "b"]  # 无冲突簇不动


def test_rar_cross_attribute_shared_word_no_false_conflict() -> None:
    # v3 复核反例（2026-09-06）：共享 'works' 但属性不同（雇主 vs 兄弟雇主）——
    # 不得判冲突重排（旧条留在原位，新条不翻前）
    a = _mem("a", "Alice", 1_646_000_000_000, "Alice works at Google", "Google")
    b = _mem("b", "Alice", 1_676_000_000_000, "Alice's brother works at Microsoft", "brother")
    out = arbitrate([(a, 1.0), (b, 1.0)], top_n=50)
    assert [m.id for m, _s in out] == ["a", "b"]


def test_rar_e2e_newest_surfaces_first() -> None:
    with tempfile.TemporaryDirectory(prefix="rar-") as td:
        settings = Settings(data_dir=Path(td), embedding_provider="hash",
                            scene="aml", journal_mode="off", _env_file=None,
                            governance_enabled=False,  # RAR 独立于治理：两版并存
                            recency_arbitration_enabled=True)
        store = SQLiteMemoryStore(str(settings.data_dir / "cortexa.db"),
                                  embedding_dim=settings.embedding_dim)
        try:
            llm = LLMClient(settings)
            for content, ts in (("Team Nova selected Vendor A in 2022", 1_646_000_000_000),
                                ("Team Nova selected Vendor B in 2024", 1_676_000_000_000)):
                req = AddRequest(request_id="r", scope="s", session_id="s",
                                 messages=[AmlMessage(role="user", content=content,
                                                       timestamp=ts)])
                run_add(store, llm, settings, req, journal=None)
            res = search_memory(store, llm, settings,
                                query="which vendor did Team Nova select",
                                scope="s", top_k=5)
            contents = [r.content for r in res]
            assert any("2024" in c for c in contents) and any("2022" in c for c in contents)
            i_new = next(i for i, c in enumerate(contents) if "2024" in c)
            i_old = next(i for i, c in enumerate(contents) if "2022" in c)
            assert i_new < i_old  # recency 仲裁：最新版本排前
        finally:
            store.close()