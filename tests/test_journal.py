"""任务流水（TaskJournal）测试：事件留痕 / 模式脱敏 / FSRS 检索强化 / 孤岛统计 / 锁同源。

业务锚点：
- 官方红线"评测数据不留日志" -> eval 模式内容脱敏必须有测试锁死；
- 测试效应（Science 2008）-> 检索命中即强化（fsrs 更新）必须有测试锁死；
- Obsidian 孤岛节点启发 -> orphan_ratio 统计口径与 count() 同源（status='ACTIVE'）。
- 连接与锁同源 -> 同一个 sqlite3 连接被两把互不相干的 RLock 保护等于无保护（并发 500
  根因，见 TestSharedConnectionLock）。
"""

from __future__ import annotations

import concurrent.futures as cf
from pathlib import Path

import pytest

import cortexa.server as server_module
from cortexa import Cortexa
from cortexa.config import Settings
from cortexa.journal import TaskJournal
from cortexa.models import MemoryInstance, content_hash
from cortexa.server import create_app
from cortexa.storage import SQLiteMemoryStore


@pytest.fixture()
def cm(tmp_path: Path) -> Cortexa:
    with Cortexa(data_dir=tmp_path / "lib") as facade:
        yield facade


class TestJournalEvents:
    """add/search 全链路事件留痕：'进行什么记忆任务时，做到了哪些'。"""

    def test_add_event_records_stored_and_types(self, cm: Cortexa) -> None:
        cm.remember("这个项目的包管理器统一用 pnpm，不要用 npm", scope="code")
        add_events = cm.journal.events("add", scope="code")
        assert len(add_events) == 1
        detail = add_events[0].detail
        assert detail["stored"] == 1
        assert detail["duplicates"] == 0
        assert detail["types"] == ["RULE"]  # 识别分流结果可回放

    def test_search_event_records_hits_and_latency(self, cm: Cortexa) -> None:
        cm.remember("林晚在第三章把怀表交给了沈屹", scope="novel")
        hits = cm.recall("怀表交给了谁", scope="novel")
        assert hits  # 前置：检索本身有效
        evs = cm.journal.events("search", scope="novel")
        assert len(evs) == 1
        detail = evs[0].detail
        assert detail["query"] == "怀表交给了谁"
        assert [h["id"] for h in detail["hits"]] == [h.id for h in hits]
        assert detail["latency_ms"] >= 0
        # P2-5b：候选池通道来源随结果落库——每个命中可回答"从哪个通道来"
        assert detail["hits"] and all(
            isinstance(h.get("channels"), list) and h["channels"]
            and set(h["channels"]) <= {"fts", "vec", "sem", "chapter", "entity"}
            for h in detail["hits"]
        )

    def test_scope_filtered_tail(self, cm: Cortexa) -> None:
        cm.remember("novel note", scope="novel")
        cm.remember("code note", scope="code")
        assert all(e.scope == "novel" for e in cm.journal.tail(50, scope="novel"))
        assert len(cm.journal.tail(50, scope="novel")) == 1

    def test_summary_counts(self, cm: Cortexa) -> None:
        cm.remember("a", scope="s")
        cm.recall("a", scope="s")
        summary = cm.journal.summary(scope="s")
        assert summary["by_event_type"]["add"]["count"] == 1
        assert summary["by_event_type"]["search"]["count"] == 1
        assert summary["last_event_ts_ms"] is not None


class TestJournalModes:
    """三种模式：app 全量 / eval 脱敏（官方合规红线）/ off 零记录。"""

    def test_eval_mode_redacts_content(self, tmp_path: Path) -> None:
        settings = Settings(
            data_dir=tmp_path / "eval", journal_mode="eval", _env_file=None
        )
        # Settings 校验器对 data_dir 的处理与 facade 一致，直接手工装配
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = SQLiteMemoryStore(
            db_path=str(settings.data_dir / "cortexa.db"),
            embedding_dim=settings.embedding_dim,
        )
        try:
            journal = TaskJournal(store.connection, mode=settings.journal_mode,
                                  lock=store.lock)
            journal.record(
                "search",
                scope="s",
                query="secret plot twist about the watch",
                hits=[{"id": "abc123", "score": 0.5}],
                top_k=5,
            )
            entry = journal.tail(1)[0]
            assert entry.detail["query"] == "<len=33>"  # 内容脱敏，只留长度
            assert entry.detail["hits"][0]["id"] == "abc123"  # 元数据保留
        finally:
            store.close()

    def test_off_mode_records_nothing(self, tmp_path: Path) -> None:
        settings = Settings(data_dir=tmp_path / "off", journal_mode="off", _env_file=None)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = SQLiteMemoryStore(
            db_path=str(settings.data_dir / "cortexa.db"),
            embedding_dim=settings.embedding_dim,
        )
        try:
            journal = TaskJournal(store.connection, mode="off", lock=store.lock)
            journal.record("add", scope="s", stored=1)
            assert journal.tail(10) == []
            assert journal.summary(scope="s")["by_event_type"] == {}
        finally:
            store.close()

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValueError):
            TaskJournal(conn=None, mode="verbose")


class TestFsrsRetrievalReinforcement:
    """检索命中即强化（测试效应）：命中的记忆 FSRS 更新，未命中的不动。"""

    def test_hit_memory_gets_reviewed(self, cm: Cortexa) -> None:
        cm.remember("Never deploy to production on Friday afternoon", scope="s")
        cm.remember("An unrelated novel fact about a pocket watch", scope="s")
        hits = cm.recall("when can we deploy?", scope="s")
        assert hits
        with cm._store.connection as conn:
            row = conn.execute(
                "SELECT fsrs_json FROM memories WHERE id = ?", (hits[0].id,)
            ).fetchone()
        fsrs = row["fsrs_json"]
        assert '"last_review_at":' in fsrs and "null" not in fsrs
        assert '"consolidation_level":1' in fsrs

    def test_missed_memory_untouched(self, cm: Cortexa) -> None:
        cm.remember("Never deploy to production on Friday afternoon", scope="s")
        cm.remember("An unrelated novel fact about a pocket watch", scope="s")
        cm.recall("when can we deploy?", scope="s")
        hit_ids = {h.id for h in cm.recall("when can we deploy?", scope="s")}
        with cm._store.connection as conn:
            rows = conn.execute(
                "SELECT id, fsrs_json FROM memories WHERE scope = 's'"
            ).fetchall()
        for r in rows:
            if r["id"] not in hit_ids:
                assert "null" in r["fsrs_json"]  # last_review_at 仍为 None


class TestOrphanStats:
    """孤岛统计（Obsidian 启发）：写入了但从未被检索命中的记忆占比。"""

    def test_orphan_ratio(self, cm: Cortexa) -> None:
        cm.remember("Never deploy to production on Friday afternoon", scope="s")
        cm.remember("A pocket watch was handed over in chapter three", scope="s")
        cm.recall("when can we deploy?", scope="s")
        stats = cm.journal.orphan_stats("s")
        assert stats["active_memories"] == 2
        assert stats["ever_hit"] == 1
        assert stats["orphans"] == 1
        assert stats["orphan_ratio"] == 0.5

    def test_empty_scope_zero_division_guard(self, cm: Cortexa) -> None:
        stats = cm.journal.orphan_stats("empty")
        assert stats["active_memories"] == 0
        assert stats["orphan_ratio"] == 0.0


class TestSharedConnectionLock:
    """连接与锁同源（2026-10-30 并发 500 根因执法）。

    实证链：公网端点 c=16 灌库出现 500 "Internal Server Error"（c=1/4/8 各 48 请求
    全 200）；本机 16 线程 × 400 次 store.add + journal.record 混合作业复现 42 次
    sqlite3 异常——cannot commit - no transaction is active ×32、SystemError: error
    return without exception set ×6、not an error ×4，栈顶均为 journal 的
    _conn.commit()。根因：TaskJournal 复用 store 的 sqlite3.Connection，却自带一把
    独立 RLock——两把互不相干的锁保护同一个非线程安全对象等于无保护。本类把
    「装配锁同源」与「并发零异常」同时钉死。
    """

    def test_injected_lock_is_the_store_lock(self, tmp_path: Path) -> None:
        store = SQLiteMemoryStore(db_path=str(tmp_path / "c.db"), embedding_dim=512)
        try:
            shared = TaskJournal(store.connection, mode="eval", lock=store.lock)
            assert shared._lock is store.lock
            # 不传则退化为独立锁（仅在另起连接时安全）——两种装配必须可区分
            solo = TaskJournal(store.connection, mode="eval")
            assert solo._lock is not store.lock
        finally:
            store.close()

    def test_facade_wires_lock(self, cm: Cortexa) -> None:
        assert cm.journal._lock is cm._store.lock

    def test_server_wires_lock(self, tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
        """参赛 HTTP 端点走的是 create_app 这条路，装配必须同样同源。"""
        seen: dict[str, object] = {}
        real_journal = server_module.TaskJournal
        real_store = server_module.SQLiteMemoryStore

        def spy_journal(conn, mode="app", lock=None):  # noqa: ANN001
            seen["lock"] = lock
            return real_journal(conn, mode=mode, lock=lock)

        def spy_store(*args, **kwargs):
            inst = real_store(*args, **kwargs)
            seen["store"] = inst
            return inst

        monkeypatch.setattr(server_module, "TaskJournal", spy_journal)
        monkeypatch.setattr(server_module, "SQLiteMemoryStore", spy_store)
        create_app(Settings(data_dir=tmp_path / "srv", embedding_provider="hash",
                            api_key="k", journal_mode="eval", _env_file=None))
        assert seen["lock"] is seen["store"].lock  # type: ignore[union-attr]

    def test_concurrent_add_and_record_raises_nothing(self, tmp_path: Path) -> None:
        """16 线程混合作业：store.add / journal.record / store.get 交错，零异常。"""
        store = SQLiteMemoryStore(db_path=str(tmp_path / "cc.db"), embedding_dim=512)
        journal = TaskJournal(store.connection, mode="eval", lock=store.lock)
        errors: list[str] = []

        def work(i: int) -> None:
            try:
                text = f"catering budget for record {i} is {500 + i} euros"
                mem = MemoryInstance(scope="u1", session_id=f"s{i % 5}", content=text,
                                     content_hash=content_hash("u1", text))
                store.add(mem)
                journal.record("add", scope="u1", session_id=f"s{i % 5}",
                               request_id=f"r{i}", content=text)
                store.get(mem.id, "u1")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        try:
            with cf.ThreadPoolExecutor(max_workers=16) as ex:
                list(ex.map(work, range(300)))
            assert not errors, f"并发混合作业出现 {len(errors)} 次异常：{errors[:3]}"
            assert journal.summary("u1")["by_event_type"]["add"]["count"] == 300
        finally:
            store.close()
