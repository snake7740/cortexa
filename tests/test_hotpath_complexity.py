"""P1-1/P1-2：热路径复杂度回归测试。

验证：
- touch_retrieval：一次调用只发 1 次 SELECT + 1 次 executemany（不再逐条 SELECT+UPDATE）；
- find_conflicts：大库逐 chunk 治理从 O(N) 次查询降到 1 次查询（行为语义 1:1 不变）。
"""

from __future__ import annotations

from pathlib import Path

from cortexa.domain.governance import GovernanceEngine
from cortexa.models import MemoryInstance, content_hash
from cortexa.schema import get_schema
from cortexa.storage import SQLiteMemoryStore


class CountingConn:
    """只读计数代理：sqlite3.Connection 为 C 不可变类型，不能 patch 类方法；
    改在 store._conn 实例属性（Python 对象可替换）挂代理，按调用计数。"""

    def __init__(self, real) -> None:  # type: ignore[no-untyped-def]
        self._real = real
        self.selects: list[str] = []
        self.executemany_batches: list[int] = []

    def execute(self, sql, params=()):  # type: ignore[no-untyped-def]
        text = str(sql)
        if text.strip().upper().startswith("SELECT"):
            self.selects.append(text)
        return self._real.execute(sql, params)

    def executemany(self, sql, seq):  # type: ignore[no-untyped-def]
        self.executemany_batches.append(len(seq))
        return self._real.executemany(sql, seq)

    def __getattr__(self, name: str):  # 其余（commit/row_factory 等）透传真实连接
        return getattr(self._real, name)


def _store(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(db_path=str(tmp_path / "g.db"), embedding_dim=8)


def _add(store: SQLiteMemoryStore, scope: str, text: str, entities: list[str] | None = None) -> MemoryInstance:
    m = MemoryInstance(scope=scope, content=text, content_hash=content_hash(scope, text),
                       entities=entities or [])
    store.add(m)
    return m


def test_touch_retrieval_single_select_batch_update(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    ids = []
    for i in range(6):
        m = _add(store, "s", f"memory number {i}")
        ids.append(m.id)
    counting = CountingConn(store._conn)
    monkeypatch.setattr(store, "_conn", counting)
    store.touch_retrieval(ids, "s")
    assert len(counting.selects) == 1  # 批量单查询：不再逐条 SELECT
    assert counting.executemany_batches == [6]  # 一次 executemany 全量更新
    # 语义不变：全部命中记忆都被复习
    monkeypatch.setattr(store, "_conn", counting._real)
    for mid in ids:
        assert store.get(mid, "s").fsrs.consolidation_level == 1  # type: ignore[union-attr]


def test_touch_no_ids_is_noop(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    counting = CountingConn(store._conn)
    monkeypatch.setattr(store, "_conn", counting)
    store.touch_retrieval([], "s")
    assert counting.selects == []  # 空列表不触发任何查询


def test_find_conflicts_single_query_on_large_scope(tmp_path: Path, monkeypatch) -> None:
    """大库：治理一次 Add 只发 1 次候选查询（P1-2 从 N+1 → 1），行为语义不变。"""
    store = _store(tmp_path)
    scope = "big"
    for i in range(600):
        _add(store, scope, f"Alice remembers unrelated fact number {i}", entities=["Alice", f"fact{i}"])
    _add(store, scope, "Alice prefers tea over coffee", entities=["Alice", "beverage"])
    engine = GovernanceEngine(store, get_schema("aml"))
    new_mem = _add(store, scope, "Alice currently prefers coffee", entities=["Alice", "beverage"])
    counting = CountingConn(store._conn)
    monkeypatch.setattr(store, "_conn", counting)
    conflicts = engine.find_conflicts(new_mem, scope)
    assert len(counting.selects) == 1
    monkeypatch.setattr(store, "_conn", counting._real)
    assert any("tea over coffee" in c.content for c in conflicts)
