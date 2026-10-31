"""读写分离回归门（2026-10-31 AML Full SEARCH_TIMEOUT 根因修复）。

判据取自 SQLite 官方 WAL 口径："readers do not block writers and a writer does
not block readers"。落地为一条可证伪、可计时的判据：

  另起一个线程持住写锁 HOLD_S 秒，在此期间——
    · Search 路径用到的每个纯读必须照常立刻返回（< 0.5·HOLD_S）；
      修复前必然挂到 HOLD_S（所有 SELECT 都在 `self._lock` 内）→ 红；
    · 写（add/add_semantic）必须仍被 HOLD_S 堵住 → 证明没有为了并发而
      削弱写串行（防复活"双锁共用一个连接导致静默丢记忆"，见 test_journal.py）。

另含内容不变判据：读写分离只动并发结构，读结果逐字节相同；只读连接带
query_only 护栏，杜绝有人误在读连接上写库。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from cortexa.models import MemoryInstance, content_hash
from cortexa.storage import SQLiteMemoryStore

DIM = 512
HOLD_S = 1.0            # 写锁被占住的时长
FAST_S = HOLD_S * 0.5   # 纯读必须远快于此


class _WriteLockHeld:
    """持锁线程上下文：进入即保证锁已被对方握住，退出即释放。"""

    def __init__(self, store: SQLiteMemoryStore) -> None:
        self._acquired = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(
            target=self._hold, args=(store,), daemon=True, name="write-lock-holder"
        )

    def _hold(self, store: SQLiteMemoryStore) -> None:
        with store.lock:
            self._acquired.set()
            self._release.wait(HOLD_S)

    def __enter__(self):
        self._thread.start()
        assert self._acquired.wait(5), "持锁线程未能取得写锁"
        return self

    def __exit__(self, *exc) -> bool:
        self._release.set()
        self._thread.join(5)
        return False


def _timed(fn):
    t0 = time.perf_counter()
    value = fn()
    return time.perf_counter() - t0, value


def _store(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(db_path=str(tmp_path / "rw.db"), embedding_dim=DIM)


def _corpus(tmp_path: Path):
    store = _store(tmp_path)
    emb = [0.1] * DIM
    last = None
    for i in range(20):
        mem = MemoryInstance(scope="u1", session_id="conv-1",
                             content=f"Mina noted item {i} about the Berlin seminar budget.",
                             content_hash=content_hash("u1", f"Mina noted item {i} about the Berlin seminar budget."))
        assert store.add(mem, embedding=emb) is True
        last = mem
    # 语义通道挂在一行真实记忆上（sem_search 会回 memories 取 mem_type/no_decay，
    # 指向不存在的 id 会被当作非 ACTIVE 跳过——那是正确行为，不是本测试的判据）。
    store.add_semantic(last.id, "u1", emb)
    return store, emb


class TestReadsDoNotWaitForWriteLock:
    """Search 路径上每一个纯读都不得依赖写锁。"""

    def test_count(self, tmp_path: Path) -> None:
        store, _ = _corpus(tmp_path)
        with _WriteLockHeld(store):
            dt, value = _timed(lambda: store.count("u1"))
        assert dt < FAST_S, f"count 被写锁拖住 {dt:.3f}s"
        assert value == 20

    def test_get_and_get_by_hash(self, tmp_path: Path) -> None:
        store, _ = _corpus(tmp_path)
        h4 = content_hash("u1", "Mina noted item 4 about the Berlin seminar budget.")
        mem_id = store.get_by_hash("u1", h4).id
        with _WriteLockHeld(store):
            dt_a, got = _timed(lambda: store.get(mem_id, "u1"))
            dt_b, byhash = _timed(lambda: store.get_by_hash("u1", h4))
        assert dt_a < FAST_S and dt_b < FAST_S
        assert got.id == mem_id and byhash.id == mem_id

    def test_fts_entity_and_chapter_channels(self, tmp_path: Path) -> None:
        store, _ = _corpus(tmp_path)
        with _WriteLockHeld(store):
            dt_fts, fts = _timed(lambda: store.fts_search("Berlin seminar budget", "u1", limit=10))
            dt_ent, _ = _timed(lambda: store.entity_search("mina", "u1", limit=10))
            dt_ch, _ = _timed(lambda: store.chapter_search([7], "u1", limit=10))
        assert dt_fts < FAST_S and dt_ent < FAST_S and dt_ch < FAST_S
        assert len(fts) == 10

    def test_vector_channel(self, tmp_path: Path) -> None:
        store, emb = _corpus(tmp_path)
        store.vector_search(emb, "u1", limit=5)  # 预热：矩阵冻结路径
        with _WriteLockHeld(store):
            dt, vec = _timed(lambda: store.vector_search(emb, "u1", limit=5))
        assert dt < FAST_S, f"vector_search 被写锁拖住 {dt:.3f}s"
        assert len(vec) == 5

    def test_sem_channel(self, tmp_path: Path) -> None:
        store, emb = _corpus(tmp_path)
        with _WriteLockHeld(store):
            dt_cnt, cnt = _timed(lambda: store.sem_count("u1"))
            dt_sem, sem = _timed(lambda: store.sem_search(emb, "u1", limit=5))
        assert dt_cnt < FAST_S and dt_sem < FAST_S
        assert cnt == 1 and len(sem) == 1  # 仅一行语义向量

    def test_active_timeline(self, tmp_path: Path) -> None:
        store, _ = _corpus(tmp_path)
        with _WriteLockHeld(store):
            dt, tl = _timed(lambda: store.active_timeline("u1"))
        assert dt < FAST_S, f"active_timeline 被写锁拖住 {dt:.3f}s"
        assert len(tl) == 20


class TestContentUnchanged:
    """读写分离只动并发结构，不动任何返回内容。"""

    def test_results_identical_with_and_without_write_lock(self, tmp_path: Path) -> None:
        store, emb = _corpus(tmp_path)

        def snapshot() -> dict:
            return {
                "count": store.count("u1"),
                "fts": [(m.id, round(s, 9)) for m, s in store.fts_search("Berlin budget", "u1", limit=8)],
                "vec": [(m.id, round(s, 9)) for m, s in store.vector_search(emb, "u1", limit=8)],
                "timeline": store.active_timeline("u1"),
            }

        free = snapshot()
        with _WriteLockHeld(store):
            held = snapshot()
        assert free == held


class TestWritePathNotWeakened:
    """写仍必须被写锁串行化。"""

    def test_add_blocks_while_write_lock_held(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mem = MemoryInstance(scope="u1", session_id="c", content="solo",
                             content_hash=content_hash("u1", "solo"))
        with _WriteLockHeld(store):
            dt, _ = _timed(lambda: store.add(mem))
        assert dt >= HOLD_S * 0.9, f"add 没有被写锁串行化（{dt:.3f}s）"
        assert store.get_by_hash("u1", mem.content_hash) is not None

    def test_journal_record_blocks_while_write_lock_held(self, tmp_path: Path) -> None:
        """journal 与 store 同源同锁的纪律不得被读侧改动破坏。"""
        from cortexa.journal import TaskJournal

        store = _store(tmp_path)
        j = TaskJournal(store.connection, mode="app", lock=store.lock)
        with _WriteLockHeld(store):
            dt, _ = _timed(lambda: j.record("search", scope="u1", query="q"))
        assert dt >= HOLD_S * 0.9, f"journal 写绕过了写锁（{dt:.3f}s）"

    def test_read_connection_cannot_write(self, tmp_path: Path) -> None:
        """只读连接必须带 query_only 护栏（防止将来有人误在读连接上写库）。"""
        store, _ = _corpus(tmp_path)
        store.count("u1")  # 触发本线程只读连接建立
        con = store._read_connection()
        assert int(con.execute("PRAGMA query_only").fetchone()[0]) == 1
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO memories (id) VALUES ('x')")


class TestConcurrentMixedLoad:
    """复刻官方失败形状：同一 scope 一边被并发 Add 灌、一边被并发 Search 查。

    上一类测试用"持锁"证明读不再依赖写锁；本条证明并发真跑起来不付出
    正确性代价：无异常、无幻影行、计数单调、Add 一返回就被别的线程读到。
    """

    def test_adds_and_searches_together_stay_correct(self, tmp_path: Path) -> None:
        store, emb = _corpus(tmp_path)          # 先铺 20 行基线
        base_ids = {m.id for m in _all_mems(store)}
        writers = 4
        per_writer = 25
        stop = threading.Event()
        errors: list[tuple[str, BaseException]] = []
        added: list[str] = []
        added_lock = threading.Lock()
        iterations = [0]  # 防空跑：读者必须真的与写者同时转过

        def _writer(w: int) -> None:
            try:
                for i in range(per_writer):
                    text = f"Writer {w} note {i}: the Atlas project deadline moved."
                    mem = MemoryInstance(scope="u1", session_id=f"conv-w{w}",
                                         content=text, content_hash=content_hash("u1", text))
                    with added_lock:
                        added.append(mem.id)  # 先登记再写：登记集必覆盖已可见集
                    assert store.add(mem, embedding=emb) is True
                    # 官方硬规则：Add 返回成功即必须可被 Search（跨线程读）
                    assert store.get(mem.id, "u1") is not None
            except BaseException as exc:  # noqa: BLE001 —— 收集后主线程判
                errors.append((f"writer{w}", exc))

        def _reader(r: int) -> None:
            seen_counts: list[int] = []
            try:
                known: set[str] = set()
                while not stop.is_set():
                    cnt = store.count("u1")
                    fts = store.fts_search("Atlas project deadline", "u1", limit=20)
                    vec = store.vector_search(emb, "u1", limit=20)
                    tl = store.active_timeline("u1")
                    store.sem_search(emb, "u1", limit=5)
                    with added_lock:
                        iterations[0] += 1
                    # 查完再取登记集：登记先于提交，故任何查询时刻可见的行必已在集内
                    with added_lock:
                        known.update(added)
                    seen_counts.append(cnt)
                    for mem, _s in fts:
                        assert mem.id in base_ids or mem.id in known, "fts 返回幻影行"
                    for mem, _s in vec:
                        assert mem.id in base_ids or mem.id in known, "vector 返回幻影行"
                    assert len(tl) == len({i for _sv, i in tl})  # 不重行
                    for _sv, mid in tl[:5]:
                        assert store.get(mid, "u1") is not None
            except BaseException as exc:  # noqa: BLE001
                errors.append((f"reader{r}", exc))
            assert all(b >= a for a, b in zip(seen_counts, seen_counts[1:])), \
                "同一读线程看到的 ACTIVE 计数回退了（读事务不一致）"

        readers = [threading.Thread(target=_reader, args=(r,), daemon=True) for r in range(8)]
        wr = [threading.Thread(target=_writer, args=(w,), daemon=True) for w in range(writers)]
        for t in readers + wr:
            t.start()
        for t in wr:
            t.join(120)
            assert not t.is_alive(), "写线程未在时限内收尾"
        stop.set()
        for t in readers:
            t.join(60)
            assert not t.is_alive(), "读线程未在时限内收尾"

        assert not errors, f"混跑抛异常：{errors[:3]}"
        assert iterations[0] > 0, "读者一次都没跑起来——本用例退化为空跑"
        assert store.count("u1") == 20 + writers * per_writer
        # 全量可读：每条 Add 都能被 get_by_hash 取回（主表/索引一致）
        for i in range(writers):
            text = f"Writer {i} note 0: the Atlas project deadline moved."
            assert store.get_by_hash("u1", content_hash("u1", text)) is not None
        # 写完后缓存与库自洽：向量缓存行数 == ids 长度 == 全库行数
        matrix = store._ensure_vec_cache()
        assert matrix.shape[0] == len(store._vec_ids) == 20 + writers * per_writer


    def test_close_releases_file_even_with_live_background_reader(self, tmp_path: Path) -> None:
        """close() 返回后库文件必须脱手，即使后台还有线程在读。

        两个方向都是真踩过的：
        · 跳线程硬关一个正在走语句的连接 → Windows fatal exception: access
          violation，进程直接没了（不是 Python 异常）；
        · 反过来不管它 → 后台线程的只读连接钉住文件，Windows 下 rmtree 报
          WinError 32（实测 12 例）。两边都堵死，中间那个点就是“语句间隙”。
        """
        db = tmp_path / "rw.db"
        store = SQLiteMemoryStore(db_path=str(db), embedding_dim=DIM)
        emb = [0.1] * DIM
        for i in range(5):
            text = f"Mina noted item {i} about the Berlin seminar budget."
            store.add(MemoryInstance(scope="u1", session_id="c", content=text,
                                     content_hash=content_hash("u1", text)), embedding=emb)
        boom = threading.Event()

        def _worker() -> None:
            try:
                while not boom.is_set():
                    store.count("u1")
                    store.active_timeline("u1")
            except BaseException:  # noqa: BLE001 —— 关库后的报错是预期结果，不上报
                boom.set()

        t = threading.Thread(target=_worker, daemon=True, name="bg-reader")
        t.start()
        time.sleep(0.05)          # 让它在自己的线程里拿到只读连接并转起来
        store.close()             # 主线程关：本线程的直关，后台的等到间隙再关
        h_deadline = time.perf_counter() + 5.0
        while time.perf_counter() < h_deadline:
            try:
                db.unlink()       # 能删 = 没有任何连接再钉着这个文件
                break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("close() 后库文件仍被连接钉住（只读连接泄漏）")
        boom.set()
        t.join(10)
        assert not t.is_alive(), "worker 未被唤醒收尾"
        assert store._read_cons == [], "已释放的连接仍被注册表强引用住"


def _all_mems(store: SQLiteMemoryStore) -> list[MemoryInstance]:
    rows = store.read_connection.execute(
        "SELECT * FROM memories WHERE scope = 'u1' AND status = 'ACTIVE'"
    ).fetchall()
    return [store._row_to_mem(r) for r in rows]
