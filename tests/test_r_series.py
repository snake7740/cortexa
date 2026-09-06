"""R 系列单测：R1 年龄衰减 / R3 内容 forget / R4 时间过滤（2026-09-06）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from cortexa.config import Settings
from cortexa.llm import LLMClient
from cortexa.models import AddRequest, AmlMessage, MemoryInstance
from cortexa.modules.content_forget import forget_matching, parse_forget
from cortexa.modules.decay import age_factor
from cortexa.modules.pipeline import run_add
from cortexa.modules.temporal_filter import apply_year_filter, year_cues
from cortexa.storage import SQLiteMemoryStore


def test_r1_age_factor_monotonic() -> None:
    now = 1_700_000_000_000
    fresh = age_factor(now, now_ms=now)
    old = age_factor(now - 365 * 86_400_000, now_ms=now)
    assert fresh == 1.0
    assert old < fresh and 0.0 < old < 1.0
    assert age_factor(None) == 1.0


def test_r3_parse_and_forget() -> None:
    assert parse_forget("Please forget my favorite color.") is not None
    assert parse_forget("forget it, let us move on") is None  # 目标空/停用词
    assert parse_forget("删除关于红色跑车的记录") is not None

    with tempfile.TemporaryDirectory(prefix="r3-") as td:
        settings = Settings(data_dir=Path(td), embedding_provider="hash",
                            scene="aml", journal_mode="off", _env_file=None)
        store = SQLiteMemoryStore(str(settings.data_dir / "cortexa.db"),
                                  embedding_dim=settings.embedding_dim)
        try:
            llm = LLMClient(settings)
            for content in ("My favorite color is blue.", "I like hiking."):
                req = AddRequest(request_id="r", scope="s", session_id="s",
                                 messages=[AmlMessage(role="user", content=content)])
                run_add(store, llm, settings, req, journal=None)
            n = forget_matching(store, "s", "favorite color")
            assert n == 1
            st = store.connection.execute(
                "SELECT status FROM memories WHERE scope=? AND content=?",
                ("s", "My favorite color is blue.")).fetchone()["status"]
            assert st == "FORGOTTEN"
        finally:
            store.close()


def test_r4_year_cues_and_filter() -> None:
    assert year_cues("When in 2022 did we meet?") == [2022]
    assert year_cues("no year here") == []
    y22 = MemoryInstance(id="a", scope="s", content="x", event_ts_ms=1_646_000_000_000)
    y23 = MemoryInstance(id="b", scope="s", content="y", event_ts_ms=1_676_000_000_000)
    rule = MemoryInstance(id="c", scope="s", content="z", event_ts_ms=1_000_000_000_000, mem_type="RULE")
    kept = apply_year_filter([(y22, 1.0), (y23, 1.0), (rule, 1.0)], [2022], set())
    ids = [m.id for m, _s in kept]
    assert ids == ["a", "c"]  # 2022 记忆 + RULE 豁免


def test_r1_r4_defaults_off_via_settings() -> None:
    cfg = Settings(_env_file=None)
    assert cfg.time_decay_enabled is False
    assert cfg.temporal_filter_enabled is False
    assert cfg.content_forget_enabled is True