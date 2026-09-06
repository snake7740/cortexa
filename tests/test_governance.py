"""GovernanceEngine 单测：写入期治理（AML 维度 D）。"""

from pathlib import Path

from cortexa.domain.governance import GovernanceEngine
from cortexa.models import MemoryInstance, MemoryStatus, content_hash
from cortexa.schema import get_schema
from cortexa.storage import SQLiteMemoryStore


def _store(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(db_path=str(tmp_path / "g.db"), embedding_dim=8)


def _mem(scope: str, content: str, entities=None, topic="") -> MemoryInstance:
    return MemoryInstance(scope=scope, content=content, entities=entities or [],
                          topic=topic, content_hash=content_hash(scope, content))


def test_preference_change_supersedes(tmp_path: Path) -> None:
    """Alice 偏好从 A 改 B：旧记忆应被 supersede。"""
    store = _store(tmp_path)
    scope = "s"
    old = _mem(scope, "Alice prefers tea over coffee", entities=["Alice"], topic="drink")
    store.add(old, embedding=None)
    new = _mem(scope, "Alice actually prefers coffee now", entities=["Alice"], topic="drink")
    store.add(new, embedding=None)
    g = GovernanceEngine(store, get_schema("aml"))
    r = g.govern(new, scope)
    assert r["superseded"] == 1
    got = store.get(old.id, scope)
    assert got.status == MemoryStatus.SUPERSEDED
    assert got.superseded_by == new.id
    store.close()


def test_unrelated_not_superseded(tmp_path: Path) -> None:
    """不同 subject 的记忆互不治理。"""
    store = _store(tmp_path)
    scope = "s"
    a = _mem(scope, "Alice likes hiking", entities=["Alice"], topic="hobby")
    store.add(a, embedding=None)
    b = _mem(scope, "Bob prefers tea", entities=["Bob"], topic="drink")
    store.add(b, embedding=None)
    g = GovernanceEngine(store, get_schema("aml"))
    r = g.govern(b, scope)
    assert r["superseded"] == 0
    assert store.get(a.id, scope).status == MemoryStatus.ACTIVE
    store.close()


def test_no_signal_no_supersede(tmp_path: Path) -> None:
    """无否定/更新信号的新陈述不取代旧（不猜测）。"""
    store = _store(tmp_path)
    scope = "s"
    a = _mem(scope, "Alice lives in Sweden", entities=["Alice"], topic="location")
    store.add(a, embedding=None)
    b = _mem(scope, "Alice visited Gothenburg last week", entities=["Alice"], topic="location")
    store.add(b, embedding=None)
    g = GovernanceEngine(store, get_schema("aml"))
    r = g.govern(b, scope)
    assert r["superseded"] == 0  # 补充信息不取代
    assert store.get(a.id, scope).status == MemoryStatus.ACTIVE
    store.close()