"""P1-4：增强/治理/写回失败观察面测试。

验证：失败计数可观测（不再静默）、失败不阻断主流程、/health 暴露。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortexa.config import Settings
from cortexa.enhance_stats import stats
from cortexa.models import MemoryInstance, content_hash
from cortexa.server import create_app
from cortexa.storage import SQLiteMemoryStore


@pytest.fixture(autouse=True)
def _clean_stats() -> None:
    stats.reset()


def test_inc_and_snapshot() -> None:
    stats.inc("rare")
    stats.inc("governance")
    snap = stats.snapshot()
    assert snap["counts"]["rare"] == 1
    assert snap["counts"]["governance"] == 1
    assert snap["total"] == 2
    assert snap["last_error"] == {}


def test_inc_records_last_error() -> None:
    stats.inc("graph", err=RuntimeError("boom: connection refused"))
    snap = stats.snapshot()
    assert snap["counts"]["graph"] == 1
    assert "boom" in snap["last_error"]["graph"]


def _mk(tmp_path: Path, scope: str = "s") -> tuple[SQLiteMemoryStore, list[str]]:
    store = SQLiteMemoryStore(db_path=str(tmp_path / "t.db"), embedding_dim=8)
    ids: list[str] = []
    for i, txt in enumerate(["Alice moved to Sweden in 2019", "Alice adopted a dog named Rob"]):
        m = MemoryInstance(
            scope=scope, content=txt, content_hash=content_hash(scope, txt)
        )
        store.add(m)
        ids.append(m.id)
    return store, ids


def test_search_rare_failure_counted_and_does_not_block(tmp_path: Path, monkeypatch) -> None:
    """RARE 写回失败：检索仍返回结果，且失败进入观察面。"""
    from cortexa.llm import LLMClient
    from cortexa.modules.search import search_memory

    store, ids = _mk(tmp_path)

    def _boom(self, mem_id, scope, link_target_ids):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated rare write failure")

    monkeypatch.setattr(store, "add_links", _boom)
    # P2-5b（2026-09-06）：RARE 触发门控 = rare_enabled and novel_adapter_enabled——
    # 本用例模拟适配器开启时 RARE 写回失败，故显式开 adapter
    cfg = Settings(
        data_dir=tmp_path, embedding_provider="hash",
        novel_adapter_enabled=True, _env_file=None,
    )
    results = search_memory(
        store, LLMClient(cfg), cfg, query="Alice", scope="s", top_k=5
    )
    assert results  # 主流程不阻断
    assert stats.snapshot()["counts"]["rare"] >= 1


def test_health_exposes_enhance_stats(tmp_path: Path) -> None:
    cfg = Settings(data_dir=tmp_path / "d", embedding_provider="hash", _env_file=None)
    with TestClient(create_app(cfg)) as client:
        body = client.get("/health").json()
    assert "enhance_stats" in body
    assert "counts" in body["enhance_stats"] and "total" in body["enhance_stats"]
