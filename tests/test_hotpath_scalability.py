"""热路径可扩展性回归（2026-10-31 AML Full SEARCH_TIMEOUT 修复的验收门）。

各项成本口径已经 R5/R8/R9 重新取证（库内 161,364 行 / 132,559 ACTIVE）：
  - storage._append_vec_cache 冻结后逐条 np.vstack 整块矩阵 ⇒ 251 ms/条（服务端实测；
    按 161,364 行 × 512 维 fp32 计 ≈ 330 MB/次）
  - storage.active_timeline 每次写入使 (count,max_rowid) 失效 ⇒ 每次检索重建（本地基准 298 ms）
  - storage.vector_search 每次检索重转 scope 字符串数组 ⇒ 237 ms（本地基准）
  - governance.find_conflicts **不**是全库口径：SQL 本就带 WHERE scope=?，最大 scope
    2216 行 = 25.1 ms 查询 + 59 ms 解码。此前“2.93 s/条”与“单条 Add 持锁 3.03 s”
    是按 13.2 万行全库外推，高估 30—50 倍，已作废。

超时的真实构成（R8/R9 直读 journal，非倒推）：服务端纯计算 latency_ms 324 条
p50=7.7 s / p95=20.2 s / max=41.0 s，**超过官方 1800 s 预算的为 0 条**；搜索有效
并行度 0.08、94% 零重叠，Add 完成速率 0.63 条/秒。按 Little 定律，在申报并发 128
下搜索等待 ≈3.7 小时 ⇒ SEARCH_TIMEOUT 由排队产生，不是算得慢。

本文件只认两件事：复杂度必须掉到 O(匹配数)，且**返回内容与修复前逐位相同**。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from cortexa.domain.governance import GovernanceEngine
from cortexa.models import MemoryInstance, content_hash
from cortexa.schema import get_schema
from cortexa.storage import SQLiteMemoryStore

DIM = 8


def _store(tmp_path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(db_path=str(tmp_path / "h.db"), embedding_dim=DIM)


def _add(store: SQLiteMemoryStore, scope: str, text: str,
         entities: list[str] | None = None, vec: np.ndarray | None = None) -> MemoryInstance:
    m = MemoryInstance(scope=scope, content=text,
                       content_hash=content_hash(scope, text), entities=entities or [])
    store.add(m, embedding=(vec if vec is not None else np.arange(DIM, dtype=np.float32)).tolist())
    return m


# ---------------------------------------------------------------------------
# A. find_conflicts：不得解码整个 scope
# ---------------------------------------------------------------------------

def _reference_conflicts(engine, store, new_mem, scope):
    """修复前口径的裁判：全量取回 ACTIVE 行 + 逐行 Python 判定。"""
    subject = engine._subject_key(new_mem)
    if not subject:
        return []
    slot = engine._slot_key(new_mem)
    rows = store.connection.execute(
        "SELECT * FROM memories WHERE scope = ? AND status = ? AND id != ?",
        (scope, "ACTIVE", new_mem.id),
    ).fetchall()
    out = []
    for r in rows:
        old = store._row_to_mem(r)
        if engine._subject_key(old) != subject:
            continue
        if engine._slot_key(old) == slot or engine._overlap(old, new_mem):
            out.append(old)
    return out


@pytest.mark.parametrize("n_other", [300, 1200])
def test_find_conflicts_matches_reference_and_skips_foreign_subjects(tmp_path, n_other):
    """异主体行占满库时：结果集必须与裁判逐条相同，且解码量不随全库规模涨。"""
    store = _store(tmp_path)
    scope = "big"
    for i in range(n_other):
        _add(store, scope, f"Bob unrelated fact number {i} with padding text",
             entities=["Bob", f"fact{i}"])
    _add(store, scope, "Alice remembers an unrelated story number 1",
         entities=["Alice", "story1"])
    _add(store, scope, "Alice prefers tea over coffee", entities=["Alice", "beverage"])
    engine = GovernanceEngine(store, get_schema("aml"))
    new = _add(store, scope, "Alice currently prefers coffee", entities=["Alice", "beverage"])

    ref = [m.id for m in _reference_conflicts(engine, store, new, scope)]
    got = [m.id for m in engine.find_conflicts(new, scope)]
    assert got == ref, "冲突集与修复前口径不一致（返回内容必须零变化）"
    assert any("tea over coffee" in m.content for m in engine.find_conflicts(new, scope))

    # 复杂度断言：统计 find_conflicts 期间的 _row_to_mem 次数
    calls = {"n": 0}
    real = store._row_to_mem

    def counted(row):
        calls["n"] += 1
        return real(row)

    store._row_to_mem = counted  # type: ignore[method-assign]
    try:
        engine.find_conflicts(new, scope)
    finally:
        del store._row_to_mem  # type: ignore[attr-defined]
    assert calls["n"] <= 4 * (len(ref) + 4), (
        f"find_conflicts 解码 {calls['n']} 行，冲突仅 {len(ref)} 条 —— 仍在整库扫描"
    )


def test_find_conflicts_scales_sublinearly(tmp_path):
    """库从 400 → 3200 行（全是异主体）时，find_conflicts 耗时不得同比涨。"""
    base = None
    for n in (400, 3200):
        sub = tmp_path / f"s{n}"
        sub.mkdir(exist_ok=True)
        store = _store(sub)
        scope = "grow"
        for i in range(n):
            _add(store, scope, f"Carol unrelated item {i} padding padding padding",
                 entities=["Carol", f"attr{i}"])
        _add(store, scope, "Dave prefers tea over coffee", entities=["Dave", "beverage"])
        engine = GovernanceEngine(store, get_schema("aml"))
        new = _add(store, scope, "Dave prefers coffee now", entities=["Dave", "beverage"])
        t0 = time.perf_counter()
        engine.find_conflicts(new, scope)
        dt = time.perf_counter() - t0
        if base is None:
            base = dt
        else:
            assert dt <= max(base * 4.0, 0.05), (
                f"8 倍库规模使 find_conflicts 从 {base*1000:.1f}ms 涨到 {dt*1000:.1f}ms（仍 O(N)）"
            )


# ---------------------------------------------------------------------------
# B. 向量缓存追加：不得逐条全量拷贝，且矩阵逐位相同
# ---------------------------------------------------------------------------

def test_vec_append_is_amortized_and_matrix_bitwise_identical(tmp_path):
    store = _store(tmp_path)
    rng = np.random.default_rng(11)
    vecs = [rng.standard_normal(DIM).astype(np.float32) for _ in range(1200)]
    for i, v in enumerate(vecs):
        _add(store, "sc", f"fact {i}", vec=v)

    store.vector_search(vecs[0].tolist(), "sc", limit=3)  # 触发冻结
    frozen = store._ensure_vec_cache().copy()
    assert frozen.shape[0] == 1200

    copy_rows = getattr(store, "_vec_copy_rows", 0)
    for i, v in enumerate(vecs):
        _add(store, "sc", f"second {i}", vec=v)
    grew = getattr(store, "_vec_copy_rows", 0) - copy_rows

    mat = store._ensure_vec_cache()
    assert mat.shape[0] == 2400
    expect = np.vstack([frozen] + [v.reshape(1, -1) for v in vecs]).astype(np.float32)
    assert np.array_equal(mat, expect), "追加后矩阵与逐条 vstack 结果不逐位相同"
    assert grew <= 8 * 1200, f"1200 次追加拷贝了 {grew} 行，未摊销（应为 O(N) 总量而非 O(N²)）"


def test_vec_search_scores_match_brute_force(tmp_path):
    """hash 向量通道：scope 隔离 + 纯余弦降序 + min_cos 剪枝，逐位等于裁判实现。"""
    store = _store(tmp_path)
    rng = np.random.default_rng(5)
    scopes = ["a", "a", "b", "a", "b"]
    stored: dict[str, list[tuple[str, np.ndarray]]] = {}
    for i, sc in enumerate(scopes):
        v = rng.standard_normal(DIM).astype(np.float32)
        m = _add(store, sc, f"content {i}", vec=v)
        stored.setdefault(sc, []).append((m.id, v))
    store.vector_search(rng.standard_normal(DIM).astype(np.float32).tolist(), "a", limit=2)  # 冻结
    for i in range(4):  # 冻结后继续追加
        v = rng.standard_normal(DIM).astype(np.float32)
        m = _add(store, "a", f"later {i}", vec=v)
        stored["a"].append((m.id, v))

    for trial in range(6):
        q = rng.standard_normal(DIM).astype(np.float32)
        min_cos = 0.28
        got = store.vector_search(q.tolist(), "a", limit=10, min_cos=min_cos)
        ids = [m.id for m, _ in got]
        assert set(ids) <= {m for m, _ in stored["a"]}, "跨 scope 泄漏"
        # 裁判：同 scope 全量纯余弦
        cand = [(mid, float(np.dot(v, q) / max(np.linalg.norm(v) * np.linalg.norm(q), 1e-9)))
                for mid, v in stored["a"]]
        cand.sort(key=lambda t: -t[1])
        keep = {mid for mid, s in cand if s >= min_cos}  # RULE/no_decay 默认 False
        assert set(ids) == {mid for mid in keep if mid in {m for m, _ in cand}}, (
            f"trial{trial}: 剪枝集合不一致 got={ids} ref={list(keep)}"
        )
        scores = [s for _, s in got]
        assert scores == sorted(scores, reverse=True), "相似度必须降序"
        by_id = dict(cand)
        for m, s in got:
            ref = by_id[m.id]
            assert abs(s - ref) < 1e-6, f"{m.id} 分值漂移 {s} vs {ref}"


# ---------------------------------------------------------------------------
# C. active_timeline：增量维护必须等于全量重建
# ---------------------------------------------------------------------------

def test_active_timeline_incremental_equals_full_rebuild(tmp_path):
    store = _store(tmp_path)
    scope = "tl"
    ids: list[str] = []
    for i in range(200):
        ids.append(_add(store, scope, f"turn {i}", vec=np.zeros(DIM, np.float32)).id)
    base = store.active_timeline(scope)
    assert [x[1] for x in base] == ids

    for i in range(200, 260):  # 纯追加：不得丢行/错序
        nid = _add(store, scope, f"turn {i}", vec=np.zeros(DIM, np.float32)).id
        ids.append(nid)
    assert store.active_timeline(scope) == [(None, mid) for mid in ids], \
        "增量追加后时间线丢行/错序"

    store.supersede(ids[3], ids[5], scope)  # 结构性变更必须触发重建
    live = store.connection.execute(
        "SELECT id FROM memories WHERE scope=? AND status='ACTIVE' ORDER BY rowid", (scope,)
    ).fetchall()
    assert store.active_timeline(scope) == [(None, r[0]) for r in live], \
        "supersede 后时间线未与 ACTIVE 集对齐"


# ---------------------------------------------------------------------------
# D. 观测面：新增计数器必须存在
# ---------------------------------------------------------------------------

def test_store_exposes_scalability_counters(tmp_path):
    store = _store(tmp_path)
    assert hasattr(store, "_vec_copy_rows")
    assert store._vec_copy_rows == 0
