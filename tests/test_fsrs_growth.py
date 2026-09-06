"""P1-6：FSRS「用得越多越牢」最小稳定性增长测试。

验证：成功检索使 stability 增长（S'=S×1.15，封顶）；记忆越巩固，同一时长后
可提取率 R(t) 越高（衰减显著放缓）；幂等路径不破坏既有契约。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from cortexa.models import MemoryInstance, content_hash
from cortexa.storage import FSRS_S_CAP_DAYS, FSRS_S_GAIN, SQLiteMemoryStore


def _mk(store: SQLiteMemoryStore, scope: str = "s", text: str = "alpha fact") -> MemoryInstance:
    m = MemoryInstance(scope=scope, content=text, content_hash=content_hash(scope, text))
    store.add(m)
    return m


def _fsrs(store: SQLiteMemoryStore, mem: MemoryInstance) -> object:
    return store.get(mem.id, mem.scope).fsrs  # type: ignore[union-attr]


def test_successful_touches_grow_stability(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(db_path=str(tmp_path / "t.db"), embedding_dim=8)
    m = _mk(store)
    assert _fsrs(store, m).stability == 1.0
    store.touch_retrieval([m.id], m.scope)
    assert _fsrs(store, m).stability == pytest.approx(FSRS_S_GAIN)
    store.touch_retrieval([m.id], m.scope)
    assert _fsrs(store, m).stability == pytest.approx(FSRS_S_GAIN ** 2)
    # 巩固等级同样递增（旧语义保持）
    assert _fsrs(store, m).consolidation_level == 2


def test_stability_growth_capped(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(db_path=str(tmp_path / "t2.db"), embedding_dim=8)
    m = _mk(store)
    # 从接近上限的 S 开始反复命中：不越过封顶
    store.connection.execute(
        "UPDATE memories SET fsrs_json=? WHERE id=? AND scope=?",
        (
            _fsrs(store, m).model_copy(update={"stability": FSRS_S_CAP_DAYS}).model_dump_json(),
            m.id, m.scope,
        ),
    )
    store.connection.commit()
    store.touch_retrieval([m.id], m.scope)
    assert _fsrs(store, m).stability <= FSRS_S_CAP_DAYS


def test_more_reviews_decay_slower(tmp_path: Path) -> None:
    """同一遗忘时长下，复习次数多的记忆可提取率更高（衰减显著放缓）。"""
    store = SQLiteMemoryStore(db_path=str(tmp_path / "t3.db"), embedding_dim=8)
    a = _mk(store, text="never reviewed fact")
    b = _mk(store, text="reviewed five times fact")
    store.touch_retrieval([b.id], b.scope)
    store.touch_retrieval([b.id], b.scope)
    store.touch_retrieval([b.id], b.scope)
    store.touch_retrieval([b.id], b.scope)
    store.touch_retrieval([b.id], b.scope)
    fa, fb = _fsrs(store, a), _fsrs(store, b)
    assert fb.stability > fa.stability
    elapsed = 9 * 86400  # 9 天
    now_a = (fa.last_review_at or time.time()) + elapsed
    now_b = (fb.last_review_at or time.time()) + elapsed
    # a 只复习一次（now_a 取 last_review_at+9d → R≈0.5）；b 复习 5 次 S 大 → R 更高
    assert fa.retrievability(now=now_a) < fb.retrievability(now=now_b)
