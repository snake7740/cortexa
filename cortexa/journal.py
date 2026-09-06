"""任务流水（Journal）：记忆任务的可回放审计层。

业务定位：回答"进行什么记忆任务时，做到了哪些"——
- 小说撰写：每次写入（剧情事件/人物状态）与检索（查了哪些旧线索）全留痕，
  支撑几十万字的线索一致性核查；
- 代码开发：决策/commit 记忆的写入与使用记录，形成项目活记忆账本；
- 参赛评测：eval 模式自动脱敏（query/memory 等内容字段只留长度），
  合规官方"评测数据不留日志"红线。

三种模式（Settings.journal_mode）：
- off  ：不记录（record 零开销短路）
- app  ：全量记录（默认，真实使用）
- eval ：记录元数据，内容字段脱敏（参赛评测模式）

存储：复用 MemoryStore 的 SQLite 连接（journal 表在 _SCHEMA 中），单连接单锁，
与记忆写入同事务上下文。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field

# eval 模式下需要脱敏的内容字段（其余元数据——id/分数/耗时/计数——保留）
_REDACT_KEYS = frozenset({"query", "memory", "content", "text"})

MODES = ("off", "app", "eval")


@dataclass(frozen=True)
class JournalEntry:
    """单条流水事件。detail 为事件载荷（已按模式脱敏）。"""

    ts_ms: int
    event_type: str
    scope: str | None
    session_id: str | None
    request_id: str | None
    detail: dict = field(default_factory=dict)


class TaskJournal:
    """写入/检索事件的流水账。mode 决定记录深度（off/app/eval）。"""

    def __init__(self, conn: sqlite3.Connection, mode: str = "app") -> None:
        if mode not in MODES:
            raise ValueError(f"journal_mode must be one of {MODES}, got {mode!r}")
        self._conn = conn
        self.mode = mode
        self._lock = threading.RLock()

    # -- 写入 ---------------------------------------------------------------

    def record(
        self,
        event_type: str,
        *,
        scope: str | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        **detail: object,
    ) -> None:
        """记录一条事件。eval 模式自动脱敏内容字段；off 模式零开销短路。"""
        if self.mode == "off":
            return
        safe_detail = self._sanitize(detail)
        with self._lock:
            self._conn.execute(
                "INSERT INTO journal (ts_ms, event_type, scope, session_id, request_id, detail_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    int(time.time() * 1000),
                    event_type,
                    scope,
                    session_id,
                    request_id,
                    json.dumps(safe_detail, ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def _sanitize(self, detail: dict) -> dict:
        if self.mode != "eval":
            return detail
        return {
            k: (f"<len={len(v)}>" if k in _REDACT_KEYS and isinstance(v, str) else v)
            for k, v in detail.items()
        }

    # -- 查询 ---------------------------------------------------------------

    def tail(self, n: int = 20, *, scope: str | None = None) -> list[JournalEntry]:
        """最近 n 条事件（可按 scope 过滤），时间降序。"""
        return self._query(limit=n, scope=scope)

    def events(
        self,
        event_type: str | None = None,
        *,
        scope: str | None = None,
        limit: int | None = 100,
    ) -> list[JournalEntry]:
        """按事件类型/ scope 过滤查询，时间降序；limit=None 为全量扫描。"""
        return self._query(limit=limit, scope=scope, event_type=event_type)

    def summary(self, scope: str | None = None) -> dict:
        """任务概况：各事件类型计数 + 最近活动时间（"做到了哪些"的一眼视图）。"""
        with self._lock:
            if scope is None:
                rows = self._conn.execute(
                    "SELECT event_type, COUNT(*), MAX(ts_ms) FROM journal GROUP BY event_type"
                ).fetchall()
                last = self._conn.execute("SELECT MAX(ts_ms) FROM journal").fetchone()[0]
            else:
                rows = self._conn.execute(
                    "SELECT event_type, COUNT(*), MAX(ts_ms) FROM journal "
                    "WHERE scope = ? GROUP BY event_type",
                    (scope,),
                ).fetchall()
                last = self._conn.execute(
                    "SELECT MAX(ts_ms) FROM journal WHERE scope = ?", (scope,)
                ).fetchone()[0]
        return {
            "mode": self.mode,
            "by_event_type": {r[0]: {"count": r[1], "last_ts_ms": r[2]} for r in rows},
            "last_event_ts_ms": last,
        }

    def orphan_stats(self, scope: str, *, scan_limit: int | None = None) -> dict:
        """孤岛记忆统计（Obsidian Graph View 孤岛节点启发，2026-08-31 对齐讨论）。

        口径：活跃记忆中，从未出现在 search 事件命中列表里的比例。
        默认全量扫描（scan_limit=None）：固定限额在长篇场景下会把早章命中
        当孤岛误报（1737 条规模实证）；需要快照时显式传 scan_limit。
        孤岛多 = 编码质量差 / 记忆冗余 / 检索词不匹配——真实使用反馈的核心信号，
        对应参赛优化里的"写入了但召回不到"问题定位。
        """
        hit_ids: set[str] = set()
        for entry in self.events("search", scope=scope, limit=scan_limit):
            for h in entry.detail.get("hits", []):
                if isinstance(h, dict) and "id" in h:
                    hit_ids.add(h["id"])
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM memories WHERE scope = ? AND status = 'ACTIVE'", (scope,)
            ).fetchall()
        active_ids = {r[0] for r in rows}
        orphans = active_ids - hit_ids
        total = len(active_ids)
        return {
            "scope": scope,
            "active_memories": total,
            "ever_hit": len(active_ids & hit_ids),
            "orphans": len(orphans),
            "orphan_ratio": round(len(orphans) / total, 3) if total else 0.0,
            "scan_limit": scan_limit,
        }

    # -- 内部 ---------------------------------------------------------------

    def _query(
        self,
        *,
        limit: int | None,
        scope: str | None = None,
        event_type: str | None = None,
    ) -> list[JournalEntry]:
        sql = "SELECT ts_ms, event_type, scope, session_id, request_id, detail_json FROM journal"
        conds: list[str] = []
        params: list[object] = []
        if scope is not None:
            conds.append("scope = ?")
            params.append(scope)
        if event_type is not None:
            conds.append("event_type = ?")
            params.append(event_type)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY ts_ms DESC, id DESC"
        params = list(params)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            JournalEntry(
                ts_ms=r[0],
                event_type=r[1],
                scope=r[2],
                session_id=r[3],
                request_id=r[4],
                detail=json.loads(r[5]),
            )
            for r in rows
        ]
