"""Cortexa 存储抽象层（ADR-0001：嵌入式三合一 MVP 实现）。

- 元数据 + FSRS 状态：SQLite 单文件
- 精确通道：SQLite FTS5（维度 A 双通道之一，防语义漂移）
- 语义通道：向量以 float32 BLOB 落库，查询时载入内存 numpy 余弦（Phase 2 可整体
  替换为 Qdrant/Neo4j，接口不变）

硬约束（官方契约 + 业务文档）：
- scope 隔离：所有读写必须按 scope 过滤（官方：检索隔离以 scope 为准）
- RULE 记忆永不衰减/静默遗忘（维度 F 红线）；supersede 属于治理更新，允许
- content_hash 在 scope 内唯一（L2 感官过滤去重）
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from typing import Sequence

import numpy as np

from .models import FSRSState, MemoryInstance, MemoryStatus, MemoryType
from .modules.graph import init_graph_schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    session_id TEXT,
    role TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mem_type TEXT NOT NULL,
    status TEXT NOT NULL,
    event_ts_ms INTEGER,
    created_ts_ms INTEGER NOT NULL,
    valid_from_ms INTEGER,
    valid_to_ms INTEGER,
    superseded_by TEXT,
    wing TEXT NOT NULL DEFAULT 'general',
    room TEXT NOT NULL DEFAULT '',
    entities_json TEXT NOT NULL DEFAULT '[]',
    topic TEXT NOT NULL DEFAULT '',
    intent TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    no_decay INTEGER NOT NULL DEFAULT 0,
    retrieval_weight REAL NOT NULL DEFAULT 1.0,
    fsrs_json TEXT NOT NULL DEFAULT '{}',
    links_json TEXT NOT NULL DEFAULT '[]',
    source_request_id TEXT,
    op_type TEXT NOT NULL DEFAULT 'add',
    supersedes_id TEXT,
    UNIQUE (scope, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_memories_scope_status ON memories (scope, status);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    mem_id UNINDEXED,
    scope UNINDEXED
);
CREATE TABLE IF NOT EXISTS vectors (
    mem_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    dim INTEGER NOT NULL,
    embedding BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    scope TEXT,
    session_id TEXT,
    request_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_journal_scope_ts ON journal (scope, ts_ms);
CREATE INDEX IF NOT EXISTS idx_journal_type_ts ON journal (event_type, ts_ms);

-- 领域适配器：实体生命周期当前状态
CREATE TABLE IF NOT EXISTS entity_states (
    scope TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    status TEXT NOT NULL,
    first_chapter INTEGER NOT NULL,
    last_chapter INTEGER NOT NULL,
    appearance_count INTEGER NOT NULL DEFAULT 0,
    current_holder TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    updated_ts_ms INTEGER NOT NULL,
    PRIMARY KEY (scope, entity_name)
);

-- 领域适配器：生命周期事件历史（不可变审计链）
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    chapter INTEGER NOT NULL,
    evidence TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    ts_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_entity
    ON lifecycle_events(scope, entity_name, chapter);

-- 领域适配器：文笔特征时序
CREATE TABLE IF NOT EXISTS style_profiles (
    scope TEXT NOT NULL,
    chapter INTEGER NOT NULL,
    profile_json TEXT NOT NULL,
    created_ts_ms INTEGER NOT NULL,
    PRIMARY KEY (scope, chapter)
);
"""


class RuleImmortalError(RuntimeError):
    """违反「规则记忆永不遗忘」红线（维度 F）。衰减遗忘与静默遗忘均须拒绝。"""


class MemoryStore(ABC):
    """存储抽象接口：Phase 2 可替换为 Qdrant/Neo4j/PostgreSQL 组合实现。"""

    @abstractmethod
    def add(self, mem: MemoryInstance, embedding: list[float] | None = None) -> bool: ...

    @abstractmethod
    def get(self, mem_id: str, scope: str) -> MemoryInstance | None: ...

    @abstractmethod
    def fts_search(self, query: str, scope: str, limit: int) -> list[tuple[MemoryInstance, float]]: ...

    @abstractmethod
    def vector_search(self, embedding: list[float], scope: str, limit: int) -> list[tuple[MemoryInstance, float]]: ...

    @abstractmethod
    def entity_search(self, query: str, scope: str, limit: int) -> list[tuple[MemoryInstance, float]]: ...

    @abstractmethod
    def chapter_search(self, chapter_nos: list[int], scope: str, limit: int) -> list[tuple[MemoryInstance, float]]: ...

    @abstractmethod
    def count(self, scope: str | None = None) -> int: ...

    @abstractmethod
    def active_timeline(self, scope: str) -> list[tuple[str | None, str]]: ...

    @abstractmethod
    def supersede(self, old_id: str, new_id: str, scope: str) -> None: ...

    @abstractmethod
    def forget(self, mem_id: str, scope: str) -> None: ...

    @abstractmethod
    def clear_scope(self, scope: str) -> int: ...


# FTS 精确通道英文停用词（2026-09-04 维度 G precision 修复）：
# 高频虚词 IDF≈0，OR MATCH 命中即 bm25=0——弱匹配与强匹配无法区分，
# 不可答问题也返回噪声记忆。查询侧过滤后这些词不再产生匹配（向量通道的
# min_cos 已做语义剪枝，此处补齐 FTS 通道的对应门槛）。中文 bigram 不受影响。
_FTS_EN_STOP = frozenset({
    # 冠词/代词
    "the", "a", "an", "this", "that", "these", "those", "it", "its",
    "i", "you", "he", "she", "we", "they", "my", "your", "his", "her",
    "our", "their", "me", "him", "them", "us",
    # 疑问/连接/介词/助动词（查询词中占比最高的噪声源）
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does",
    "did", "have", "has", "had", "can", "could", "will", "would", "shall",
    "should", "may", "might", "must", "not", "no", "yes",
    "of", "in", "on", "at", "to", "from", "for", "with", "by", "about",
    "as", "into", "over", "under", "than", "then", "and", "or", "but",
    "if", "else", "so", "too", "also", "very", "just",
    # 通用疑问句填充词（低信息）
    "doe", "done", "want", "know", "tell", "please",
})


# ---- FSRS 稳定性增长常量（P1-6，2026-09-06）----
FSRS_S_GAIN = 1.15  # 每次成功检索 stability × 1.15（R(t)=（1+t/(9S))^-1 → S 越大衰减越慢）
FSRS_S_CAP_DAYS = 3650.0  # 封顶 10 年，防重复命中导致 S 失控


def _fts_escape(query: str) -> str:
    """把自然语言查询转成 FTS5 安全的 OR 词元匹配（防语法错误与注入）。

    CJK 连续段：FTS5 unicode61 不切分中文，这里外部按二元组（bigram）切分，
    保证中文 query 与内容共享词汇时可命中精确通道（真实中文使用反馈驱动）。
    """
    raw = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if t]
    expanded: list[str] = []
    for token in raw:
        cjk_runs = re.findall(r"[\u4e00-\u9fff]+", token)
        expanded.extend(re.sub(r"[\u4e00-\u9fff]+", " ", token).split())
        for run in cjk_runs:
            if len(run) == 1:
                expanded.append(run)
            else:
                expanded.extend(run[i : i + 2] for i in range(len(run) - 1))
    uniq: list[str] = []
    seen: set[str] = set()
    for t in expanded:
        if t.lower() in _FTS_EN_STOP:
            continue  # 高频虚词无区分力（维度 G precision 修复）
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    if not uniq:
        return ""
    return " OR ".join(f'"{t}"' for t in uniq)


def _fts_index_text(content: str) -> str:
    """FTS 索引文本：原文 + CJK 二元组展开（与查询侧 _fts_escape 对称切分）。

    若只切查询侧，unicode61 把索引里的 CJK 连续段当单个 token，bigram 永远
    无法精确匹配——两侧必须同规则。旧索引库需重建（MVP 阶段直接删库重建）。
    """
    parts = [content]
    for run in re.findall(r"[\u4e00-\u9fff]+", content):
        if len(run) > 1:
            parts.append(" ".join(run[i : i + 2] for i in range(len(run) - 1)))
    return " ".join(parts)


SUBJECT_SQL_EXPR = (
    "CASE WHEN json_extract(entities_json,'$[0]') IS NOT NULL "
    "THEN CAST(json_extract(entities_json,'$[0]') AS TEXT) "
    "WHEN topic IS NOT NULL AND topic<>'' THEN topic "
    "ELSE substr(content,1,6) END"
)


class _ReadRows:
    """在闸门内取完的只读结果：调用方拿到已固化的行，连接上不留活动语句。

    为什么必须固化：SQLite 只允许在没有任何活动语句时真正关闭连接。若 execute()
    出闸门后才由调用方 fetch，close() 就可能落在“已 execute、未 fetch”的窗口里——
    sqlite3_close 返回 SQLITE_BUSY、连接实际没关，却被当作回收成功从注册表摘掉，
    于是文件句柄永久被钉住（实测 WinError 32），注册表还显示干净，属于假绿。
    """

    __slots__ = ("_rows",)

    def __init__(self, rows: list[sqlite3.Row]) -> None:
        self._rows = rows

    def fetchone(self) -> sqlite3.Row | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[sqlite3.Row]:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


class _ReadConn:
    """只读连接的包装：每条连接自带一把“语句闸门”锁。

    为什么不能直接关一个可能正被别的线程用的 sqlite3 连接：实测在另一个线程
    走语句中间关它，会得到 Windows fatal exception: access violation——进程直接
    没了，不是 Python 异常。所以连接只能在这两条语句之间的间隙被回收：
      · 使用方：execute 持锁完成“执行 + 取完行”，出了闸门连接上必无活动语句；
      · close()：拿得到闸门才真关，且只在真关掉后才认领；拿不到或关不掉就返回
        False，由调用方继续持有，绝不留下“没关却登记成已死”的泄漏；
      · 关闭后重入 execute 直接报错，不会悄悄复用到半死的连接。
    """

    __slots__ = ("_con", "_gate", "_dead")

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con
        self._gate = threading.Lock()
        self._dead = False

    def execute(self, sql: str, params: Sequence = ()) -> _ReadRows:  # noqa: ANN001
        with self._gate:
            if self._dead:
                raise sqlite3.ProgrammingError("store already closed")
            return _ReadRows(self._con.execute(sql, params).fetchall())

    def release(self, wait_s: float) -> bool:
        """尝试安全回收：等到闸门就关掉，等不到/关不掉返回 False（调用方自己收尾）。"""
        if not self._gate.acquire(timeout=wait_s):
            return False
        try:
            self._con.close()
        except sqlite3.Error:
            self._gate.release()
            return False  # 没真的关掉，不谎报回收成功
        self._dead = True
        self._gate.release()
        return True


class SQLiteMemoryStore(MemoryStore):
    """MVP 嵌入式实现：SQLite + FTS5 + 内存 numpy 向量余弦。"""

    # 向量缓存页大小（PagedAttention 思想）：分页期追加只写页槽，冻结期走容量摊销
    _VEC_PAGE_SIZE = 256
    # 冻结矩阵容量增长率：追加填满时按 2 倍扩容，全库累计拷贝 O(N) 而非 O(N²)
    _VEC_GROW_NUMERATOR = 2

    def __init__(self, db_path: str, embedding_dim: int = 512) -> None:
        self._dim = embedding_dim
        self._db_path = db_path
        # 读写分离（2026-10-31 AML Full SEARCH_TIMEOUT 根因修复）：
        # _lock 回归它的本义——只串行写入。SQLite 官方 WAL 口径是"读者不挡写者、
        # 写者不挡读者，同时只有一个写者"，而原实现把每条纯 SELECT 也关进 _lock，
        # 等于自己把这个能力关掉了：64 路 Add 洪峰下 Search 被挤到 60 s+（实测）。
        # 纯读改走线程本地只读连接（见 _read_connection），不再排队等写锁。
        # _cache_lock 专管进程内向量/时间线缓存；锁序固定 _lock -> _cache_lock，
        # 读侧永不取 _lock，故不构成死锁环。
        self._cache_lock = threading.RLock()
        self._lock = threading.RLock()
        self._read_local = threading.local()
        self._read_cons: list[tuple[int, _ReadConn]] = []
        self._closed = False
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        init_graph_schema(self._conn)  # 实体关系图表（Palantir link type 纪律）
        self._init_subject_lookup()  # 治理候选查询的表达式索引（2026-10-31 根因修复）
        # P0-3：scope 元数据表（首末写入时间/来源），30 天 TTL 合规清扫的依据
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS scope_meta ("
            " scope TEXT PRIMARY KEY,"
            " first_write_ms INTEGER,"
            " last_write_ms INTEGER,"
            " source TEXT NOT NULL DEFAULT 'app')"
        )
        # G1（2026-09-06）：语义向量独立表（provider/dim 与 hash 512 分离，避免混合矩阵）
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS vectors_sem ("
            " mem_id TEXT PRIMARY KEY,"
            " scope TEXT NOT NULL,"
            " dim INTEGER NOT NULL,"
            " embedding BLOB NOT NULL)"
        )
        self._migrate_memories_columns()  # 既有库补列（op_type/supersedes_id）
        self._conn.commit()
        # 向量内存缓存（懒加载，写入时同步追加）；_vec_pages 是分页待合并区，
        # _vec_matrix 非 None 表示已冻结（页已全部折入）；_vec_rows 是冻结矩阵的
        # 逻辑行数——_vec_matrix.shape[0] 可能更大（预留容量），检索一律取切片视图
        self._vec_ids: list[str] = []
        self._vec_scopes: list[str] = []
        self._vec_pages: list[list[np.ndarray]] = []
        self._vec_matrix: np.ndarray | None = None
        self._vec_rows = 0
        # 观测面：因容量增长被整段拷贝的行数（增长应 ~O(N)，不是 O(N²)）
        self._vec_copy_rows = 0
        # scope -> 该 scope 在冻结矩阵中的升序行号（替代每次检索整库重转字符串数组）
        self._scope_rows: dict[str, list[int]] = {}
        self._scope_idx_cache: dict[str, tuple[int, np.ndarray]] = {}
        # 邻接扩展时间线缓存：scope -> ((active_count, max_rowid), [(session_id, id)], 版本)
        self._timeline_cache: dict[
            str, tuple[tuple[int, int], list[tuple[str | None, str]], int]
        ] = {}
        # ACTIVE 成员集结构性变更计数：supersede/forget/clear_scope 递增
        self._active_set_version = 0

    # -- 只读连接（读写分离的读侧承载）---------------------------------------

    def _read_connection(self) -> _ReadConn:
        """本线程的只读连接：WAL 下与写连接并发读，不取写锁、不共享写事务状态。

        成熟做法取自社区共识——"别让线程池替你管 SQLite 并发，读写各自配调度"：
        读用独立连接，写只用那一条连接 + 一把锁。check_same_thread=False 只是让
        close() 能从任意线程回收句柄；连接由 threading.local 持有，实际只有创建
        它的线程会用到。query_only 是护栏：谁想在这条连接上写库，SQLite 直接报错。
        """
        con: _ReadConn | None = getattr(self._read_local, "con", None)
        if con is None:
            with self._cache_lock:
                if self._closed:
                    raise sqlite3.ProgrammingError("store already closed")
                raw = sqlite3.connect(
                    self._db_path, timeout=30, check_same_thread=False
                )
                raw.row_factory = sqlite3.Row
                raw.execute("PRAGMA query_only=ON")
                con = _ReadConn(raw)
                self._read_local.con = con
                self._read_cons.append((threading.get_ident(), con))
                self._reap_dead_read_connections()
        return con

    def _reap_dead_read_connections(self) -> None:
        """回收“主人已退”的只读连接（调用方持 `_cache_lock`）。

        必须做，不然注册表本身会把线程建好的连接强引用住：线程早就退了，
        文件句柄还不放，Windows 下直接钉住临时库目录（实测 12 例 rmtree 失败）。
        只碰主人已死的：正被活线程用的连接跳线程硬关会 access violation。
        """
        me = threading.get_ident()
        alive = {t.ident for t in threading.enumerate()}
        keep: list[tuple[int, _ReadConn]] = []
        for ident, rc in self._read_cons:
            if ident == me or ident in alive or not rc.release(0.0):
                keep.append((ident, rc))
        self._read_cons = keep

    # -- 治理主体查找可用性探测（2026-10-31 AML Full 根因修复的组成部分）------

    def _init_subject_lookup(self) -> None:
        """探测 JSON1 是否可用，决定 find_conflicts 能否走主体谓词。

        原实现对每条新记忆执行
        `SELECT * FROM memories WHERE scope=? AND status='ACTIVE' AND id!=?`
        并逐行构造 Pydantic MemoryInstance；谓词下推后只取回同主体的行。
        成本口径（R5 实测，按单个 scope 而非全库）：该 SQL 本就带 WHERE scope=?，
        最大 scope 2216 行 = 25.1 ms 查询 + 59 ms 解码。此前“2.93 s/条”是按
        132,559 行全库外推，高估 30—50 倍，已作废。
        这里**不**建表达式索引：收益只有 scope 局部几十毫秒，而 CREATE INDEX 会在
        进程启动时对线上 161,364 行整表求值表达式，与官方约 10 s 的 /health 预检
        抢时间。谓词无索引时走 scope 索引 + 行过滤，结果集与建索引后逐行相同。
        """
        self.subject_lookup_available = False
        try:
            self._conn.execute("SELECT json_extract('[]','$[0]') IS NULL").fetchone()
        except sqlite3.OperationalError:
            return  # 该 SQLite 无 JSON1：治理退回整库扫描口径（语义不变，只是慢）
        self.subject_lookup_available = True

    # -- 序列化辅助 ---------------------------------------------------------

    def _migrate_memories_columns(self) -> None:
        """既有库兼容：W1/W2 老库无 op_type/supersedes_id 列，幂等补齐。"""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(memories)")}
        if "op_type" not in cols:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN op_type TEXT NOT NULL DEFAULT 'add'"
            )
        if "supersedes_id" not in cols:
            self._conn.execute("ALTER TABLE memories ADD COLUMN supersedes_id TEXT")

    @staticmethod
    def _row_to_mem(row: sqlite3.Row) -> MemoryInstance:
        return MemoryInstance(
            id=row["id"],
            scope=row["scope"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            mem_type=MemoryType(row["mem_type"]),
            status=MemoryStatus(row["status"]),
            content_hash=row["content_hash"],
            event_ts_ms=row["event_ts_ms"],
            created_ts_ms=row["created_ts_ms"],
            valid_from_ms=row["valid_from_ms"],
            valid_to_ms=row["valid_to_ms"],
            superseded_by=row["superseded_by"],
            wing=row["wing"],
            room=row["room"],
            entities=json.loads(row["entities_json"]),
            topic=row["topic"],
            intent=row["intent"],
            confidence=row["confidence"],
            no_decay=bool(row["no_decay"]),
            retrieval_weight=row["retrieval_weight"],
            fsrs=FSRSState(**json.loads(row["fsrs_json"])),
            links=json.loads(row["links_json"]),
            source_request_id=row["source_request_id"],
            op_type=row["op_type"],
            supersedes_id=row["supersedes_id"],
        )

    @staticmethod
    def _mem_params(mem: MemoryInstance) -> tuple:
        return (
            mem.id, mem.scope, mem.session_id, mem.role, mem.content,
            mem.content_hash, mem.mem_type.value, mem.status.value,
            mem.event_ts_ms, mem.created_ts_ms, mem.valid_from_ms,
            mem.valid_to_ms, mem.superseded_by, mem.wing, mem.room,
            json.dumps(mem.entities, ensure_ascii=False), mem.topic, mem.intent,
            mem.confidence, int(mem.no_decay), mem.retrieval_weight,
            mem.fsrs.model_dump_json(), json.dumps(mem.links), mem.source_request_id,
            mem.op_type, mem.supersedes_id,
        )

    _INSERT = (
        "INSERT OR IGNORE INTO memories "
        "(id, scope, session_id, role, content, content_hash, mem_type, status, "
        "event_ts_ms, created_ts_ms, valid_from_ms, valid_to_ms, superseded_by, "
        "wing, room, entities_json, topic, intent, confidence, no_decay, "
        "retrieval_weight, fsrs_json, links_json, source_request_id, op_type, supersedes_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )

    # -- 写入 ---------------------------------------------------------------

    def add(self, mem: MemoryInstance, embedding: list[float] | None = None) -> bool:
        """同步写入（官方硬规则：返回成功前必须可被 Search）。

        单事务提交：主表/FTS/向量要么全可见要么全回滚，消除
        "已提交主表但索引失败"的崩溃窗口（否则重启后精确通道永久漏检）。

        Returns True 表示新写入；False 表示 scope 内重复（L2 去重命中）。
        """
        with self._lock:
            cur = self._conn.execute(self._INSERT, self._mem_params(mem))
            inserted = cur.rowcount > 0
            if inserted:
                self._conn.execute(
                    "INSERT INTO memories_fts (content, mem_id, scope) VALUES (?,?,?)",
                    (_fts_index_text(mem.content), mem.id, mem.scope),
                )
                if embedding is not None:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO vectors (mem_id, dim, embedding) VALUES (?,?,?)",
                        (mem.id, len(embedding), np.asarray(embedding, dtype=np.float32).tobytes()),
                    )
                self._conn.commit()
            if inserted and embedding is not None:
                self._append_vec_cache(mem.id, mem.scope, embedding)
            return inserted

    def _append_vec_cache(self, mem_id: str, scope: str, embedding: list[float]) -> None:
        """写侧追加缓存：add() 已在写锁内调用，这里叠 _cache_lock 与读侧快照互斥。"""
        with self._cache_lock:
            self._append_vec_cache_locked(mem_id, scope, embedding)

    def _append_vec_cache_locked(self, mem_id: str, scope: str, embedding: list[float]) -> None:
        vec = np.asarray(embedding, dtype=np.float32)
        if self._vec_matrix is None:
            page_size = self._VEC_PAGE_SIZE
            if not self._vec_pages or len(self._vec_pages[-1]) >= page_size:
                self._vec_pages.append([vec])
            else:
                self._vec_pages[-1].append(vec)
            self._vec_ids.append(mem_id)
            self._vec_scopes.append(scope)
            self._scope_rows.setdefault(scope, []).append(len(self._vec_ids) - 1)
            return

        # 已冻结（检索发生过）后的追加：容量摊销，取代原先逐次 np.vstack 全量拷贝。
        # 线上 161,364 行 ×512 维 float32 = 315 MB/条（实测 251 ms），且在全局
        # RLock 内；累积拷贝量 ~N²/2 × 2 KB = 26 TB。改为预留容量后按 2 倍扩容，
        # 全库累计拷贝降为 O(N)，单条追加降为 O(1) 摊销。
        rows = self._vec_rows
        mat = self._vec_matrix
        if mat.shape[0] <= rows:
            cap = max(rows * self._VEC_GROW_NUMERATOR, 1024)
            buf = np.empty((cap, self._dim), dtype=np.float32)
            buf[:rows] = mat[:rows]
            self._vec_copy_rows += rows
            mat = buf
        mat[rows] = vec
        self._vec_matrix = mat
        self._vec_rows = rows + 1
        self._vec_ids.append(mem_id)
        self._vec_scopes.append(scope)
        self._scope_rows.setdefault(scope, []).append(rows)
        self._scope_idx_cache.pop(scope, None)

    def _scope_row_index(self, scope: str) -> np.ndarray | None:
        """该 scope 在冻结矩阵中的升序行号（等价于 np.nonzero(scope_arr == scope)）。"""
        with self._cache_lock:
            rows = self._scope_rows.get(scope)
            if not rows:
                return None
            cached = self._scope_idx_cache.get(scope)
            if cached is not None and cached[0] == len(rows):
                return cached[1]
            arr = np.asarray(rows, dtype=np.int64)
            self._scope_idx_cache[scope] = (arr.size, arr)
            return arr

    def _rebuild_scope_rows(self) -> None:
        self._scope_rows = {}
        for i, sc in enumerate(self._vec_scopes):
            self._scope_rows.setdefault(sc, []).append(i)
        self._scope_idx_cache.clear()

    def _ensure_vec_cache(self) -> np.ndarray:
        """首次向量检索前把分页折成单一矩阵（此后追加走容量摊销）。"""
        with self._cache_lock:
            return self._ensure_vec_cache_locked()

    def _ensure_vec_cache_locked(self) -> np.ndarray:
        if self._vec_matrix is not None:
            if self._vec_rows > len(self._vec_ids):
                # 防御：矩阵逻辑行数不得越过 ids 长度（多余行无效）
                self._vec_rows = len(self._vec_ids)
            return self._vec_matrix[: self._vec_rows]
        if not self._vec_pages:
            # 冷启动：从库中全量载入（向量以 float32 落库，行格式同 numpy 内存布局）
            rows = self._read_connection().execute(
                "SELECT v.mem_id, v.embedding, m.scope FROM vectors v "
                "JOIN memories m ON m.id = v.mem_id"
            ).fetchall()
            self._vec_ids = []
            self._vec_scopes = []
            flat: list[np.ndarray] = []
            for r in rows:
                vec = np.frombuffer(r["embedding"], dtype=np.float32)
                if vec.size != self._dim:
                    continue  # 维度不符（如 provider 切换前旧数据）不入语义通道
                flat.append(vec)
                self._vec_ids.append(r["mem_id"])
                self._vec_scopes.append(r["scope"])
            if flat:
                self._vec_pages = [
                    flat[i : i + self._VEC_PAGE_SIZE]
                    for i in range(0, len(flat), self._VEC_PAGE_SIZE)
                ]
        pages = [np.asarray(p, dtype=np.float32) for p in self._vec_pages if len(p)]
        self._vec_pages = []
        self._vec_matrix = (
            np.vstack(pages) if pages else np.zeros((0, self._dim), dtype=np.float32)
        )
        self._vec_rows = self._vec_matrix.shape[0]
        if len(self._vec_ids) > self._vec_rows:
            self._vec_ids = self._vec_ids[: self._vec_rows]
            self._vec_scopes = self._vec_scopes[: self._vec_rows]
        self._rebuild_scope_rows()
        return self._vec_matrix[: self._vec_rows]

    # -- 查询 ---------------------------------------------------------------

    def get(self, mem_id: str, scope: str) -> MemoryInstance | None:
        row = self._read_connection().execute(
            "SELECT * FROM memories WHERE id = ? AND scope = ?", (mem_id, scope)
        ).fetchone()
        return self._row_to_mem(row) if row else None

    def get_by_hash(self, scope: str, content_hash: str) -> MemoryInstance | None:
        """按去重指纹定位活跃记忆（retcon 修正链的旧记忆定位入口）。

        UNIQUE(scope, content_hash) 保证至多一条；只看 ACTIVE——
        已被取代/归档的记忆不可再作为修正对象（避免套套链）。
        """
        row = self._read_connection().execute(
            "SELECT * FROM memories WHERE scope = ? AND content_hash = ? "
            "AND status = 'ACTIVE'",
            (scope, content_hash),
        ).fetchone()
        return self._row_to_mem(row) if row else None

    def count(self, scope: str | None = None) -> int:
        conn = self._read_connection()
        if scope is None:
            return conn.execute(
                "SELECT COUNT(*) FROM memories WHERE status = 'ACTIVE'"
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM memories WHERE scope = ? AND status = 'ACTIVE'",
            (scope,),
        ).fetchone()[0]

    def active_timeline(self, scope: str) -> list[tuple[str | None, str]]:
        """scope 内 ACTIVE 记忆按入库序（rowid）的 (session_id, id) 序列。

        同会话邻接扩展（2026-10-29，InvMem/AMI 证据链）的消费面：存储序即对话
        轮次序，邻居必须与种子同会话。

        2026-10-31 根因修复：原缓存仅以 (active 数, max rowid) 为失效依据，而任何
        新写都会拾高 rowid ⇒ 流式参评下每次检索全量重建 13.2 万行（本地基准 298 ms）。
        现仅当“只发生了纯追加”（无 ACTIVE 成员集结构变更，且新增行数 == 计数增量）
        时做增量拼接；其余情况（supersede/forget/clear 或计数不自洽）仍全量重建，
        结果与原实现逐条相等。

        2026-10-31 读写分离：本方法只在 `_cache_lock` 内自串行，不碰写锁。key 先取、
        ver 后取的原有序保守：写者若在这两步之间改了 ACTIVE 成员集，ver 会变新
        而全量重建；计数下降也不会满足“纯追加”条件，不会误用旧时间线。
        """
        with self._cache_lock:
            conn = self._read_connection()
            row = conn.execute(
                "SELECT COUNT(*), MAX(rowid) FROM memories "
                "WHERE scope = ? AND status = 'ACTIVE'",
                (scope,),
            ).fetchone()
            key = (int(row[0] or 0), int(row[1] or 0))
            ver = self._active_set_version
            cached = self._timeline_cache.get(scope)
            if cached is not None:
                if cached[0] == key:
                    return cached[1]
                if (
                    len(cached) > 2 and cached[2] == ver
                    and key[0] > cached[0][0] and key[1] > cached[0][1]
                ):
                    extra = conn.execute(
                        "SELECT session_id, id, rowid AS rid FROM memories "
                        "WHERE scope = ? AND status = 'ACTIVE' AND rowid > ? ORDER BY rowid",
                        (scope, cached[0][1]),
                    ).fetchall()
                    delta = key[0] - cached[0][0]
                    if len(extra) == delta:  # 自洽校验：只可能是纯追加
                        timeline = cached[1] + [(r["session_id"], r["id"]) for r in extra]
                        # 缓存键必须描述“实际取回的行”，不能用探针值：探针与 extra 分属
                        # 两次读，中间可能有写者提交（读写分离后不再被写锁隔绝）——用探针
                        # 值会把集外晚到的行记成已覆盖，下次增量重复拼接、时间线出现重行
                        # （并发混跑回归实测抓到过）。写者串行⇒rowid 无空洞，取末行即可。
                        self._timeline_cache[scope] = (
                            (len(timeline), int(extra[-1]["rid"])), timeline, ver
                        )
                        return timeline
            rows = conn.execute(
                "SELECT session_id, id, rowid AS rid FROM memories "
                "WHERE scope = ? AND status = 'ACTIVE' ORDER BY rowid",
                (scope,),
            ).fetchall()
            timeline = [(r["session_id"], r["id"]) for r in rows]
            last_rid = int(rows[-1]["rid"]) if rows else key[1]
            self._timeline_cache[scope] = ((len(timeline), last_rid), timeline, ver)
            return timeline

    def entity_search(self, query: str, scope: str, limit: int) -> list[tuple[MemoryInstance, float]]:
        """特征访问通道（维度 B 多跳的廉价前哨）：查询提到已知实体 → 直取其记忆。

        人类记忆类比：存储一直在，缺的是访问路径——正确提到某人/某物的特征
        即可唤起（无需整段语义相似）。实现为精确包含匹配（零幻觉），非相似度。
        千级规模扫描 ACTIVE 行即可；规模触发（>万条）后转独立倒排表，登记 P2。
        """
        q = query.casefold()
        rows = self._read_connection().execute(
            "SELECT * FROM memories WHERE scope = ? AND status = 'ACTIVE'",
            (scope,),
        ).fetchall()
        scored: list[tuple[MemoryInstance, int]] = []
        for r in rows:
            mem = self._row_to_mem(r)
            matched = sum(
                1 for e in mem.entities if len(e) >= 2 and e.casefold() in q
            )
            if matched:
                scored.append((mem, matched))
        scored.sort(
            key=lambda p: (
                -p[1],
                -(p[0].event_ts_ms or 0),
                p[0].id,
            )
        )
        return [(mem, float(n)) for mem, n in scored[:limit]]

    def chapter_search(
        self, chapter_nos: list[int], scope: str, limit: int
    ) -> list[tuple[MemoryInstance, float]]:
        """编号精确通道：按章节锚点前缀（【第 N 章 / 【Ch N）取回对应记忆。

        真实使用痛点：小说"第 107 章讲了什么"类查询，FTS 对孤立数字/中文数字
        命中弱；章节锚点是摄取时打上的确定性前缀，LIKE 前缀匹配零幻觉。
        命中章节数降序（提到多章时全覆盖），同分按事件时间降序。
        """
        if not chapter_nos:
            return []
        conds = " OR ".join(["m.content LIKE ?"] * 2 * len(chapter_nos))
        params: list[str] = []
        for n in chapter_nos:
            params += [f"【第{n}章%", f"【Ch {n}%"]
        rows = self._read_connection().execute(
            "SELECT m.* FROM memories m "
            f"WHERE m.scope = ? AND m.status = 'ACTIVE' AND ({conds})",
            [scope, *params],
        ).fetchall()
        mems = [self._row_to_mem(r) for r in rows]

        def _hits(mem: MemoryInstance) -> int:
            return sum(
                1
                for n in chapter_nos
                if mem.content.startswith(f"【第{n}章") or mem.content.startswith(f"【Ch {n}")
            )

        scored = sorted(
            mems,
            key=lambda m: (-_hits(m), -(m.event_ts_ms or 0), m.id),
        )
        return [(mem, float(_hits(mem))) for mem in scored[:limit]]

    def fts_search(self, query: str, scope: str, limit: int) -> list[tuple[MemoryInstance, float]]:
        """精确通道（维度 A）：FTS5 bm25，值越小越相关 -> 归一为越大越相关。"""
        match_expr = _fts_escape(query)
        if not match_expr:
            return []
        rows = self._read_connection().execute(
            "SELECT m.*, bm25(memories_fts) AS rank_score "
            "FROM memories_fts f JOIN memories m ON m.id = f.mem_id "
            "WHERE memories_fts MATCH ? AND f.scope = ? AND m.status = 'ACTIVE' "
            "ORDER BY rank_score LIMIT ?",
            (match_expr, scope, limit),
        ).fetchall()
        out: list[tuple[MemoryInstance, float]] = []
        for r in rows:
            bm25 = r["rank_score"]  # 越小越相关，可为负
            score = 1.0 / (1.0 + max(bm25, 0.0))
            out.append((self._row_to_mem(r), score))
        return out

    def vector_search(
        self, embedding: list[float], scope: str, limit: int, min_cos: float = 0.0
    ) -> list[tuple[MemoryInstance, float]]:
        """语义通道：scope 隔离 + 纯余弦相似度（降序）。

        min_cos 为语义相关性下限（precision-first）：低于阈值的候选直接剪枝，
        保证"无相关记忆"时返回空结果而非硬凑条目（官方契约允许空数组）。
        检索权重/可提取性的调制在融合层进行，本方法保持纯粹余弦语义。
        """
        # 读写分离：写锁不再挡检索。_cache_lock 内只做一件事——取一个一致快照：
        # 矩阵视图 / ids / 行号三者必须同刻捕获，否则行号可能指向写者刚追加、
        # 而本视图（行数已固定）尚不可见的行。余弦计算与取行全部在锁外完成。
        with self._cache_lock:
            matrix = self._ensure_vec_cache()
            n_rows = matrix.shape[0]
            ids = self._vec_ids
            # scope 行号索引增量维护：原实现每次检索把整库 scope 字符串重转 numpy
            # 数组（13.2 万行本地基准 237 ms）并 np.nonzero，且全程持全局 RLock。
            # _scope_rows[scope] 按升序追加，与 np.nonzero(mask)[0] 逐元素相等。
            idx = self._scope_row_index(scope)
        if not ids or n_rows == 0 or idx is None or idx.size == 0:
            return []
        if idx[-1] >= n_rows:  # 防御：同刻快照不应越界，留一手兑底
            idx = idx[idx < n_rows]
            if idx.size == 0:
                return []
        q = np.asarray(embedding, dtype=np.float32)
        sub = matrix[idx]
        qn = np.linalg.norm(q)
        subn = np.linalg.norm(sub, axis=1)
        denom = np.clip(subn * qn, 1e-9, None)
        sims = (sub @ q) / denom
        order = np.argsort(-sims)[:limit]
        # 批量取回：原实现对每条结果各发 2 次 point query（limit=k*3=300 ⇒ 600 次），
        # 两次查询 WHERE 完全同构（都要求 status='ACTIVE'），合为 1 次 IN 查询后，存
        # 在性判定与取行语义 1:1 不变（同一 mem_id 多行命中仍逐条返回，与原一致）。
        cand = [ids[idx[i]] for i in order]
        conn = self._read_connection()
        fetched: dict[str, sqlite3.Row] = {}
        for k0 in range(0, len(cand), 500):
            chunk = cand[k0:k0 + 500]
            ph = ",".join("?" for _ in chunk)
            for r in conn.execute(
                f"SELECT * FROM memories WHERE status = 'ACTIVE' AND id IN ({ph})", chunk
            ):
                if r["id"] not in fetched:
                    fetched[r["id"]] = r
        out: list[tuple[MemoryInstance, float]] = []
        for i in order:
            # RULE/no_decay 豁免语义剪枝（维度 F 红线：规则召回优先——规则是稳定
            # 约束，价值高于噪声风险；rule_boost 在融合层只加成排序不扩大召回）
            row = fetched.get(ids[idx[i]])
            if row is None:
                continue
            is_rule = row["mem_type"] == MemoryType.RULE.value or bool(row["no_decay"])
            if (not is_rule) and float(sims[i]) < min_cos:
                continue  # precision-first：普通记忆低于语义下限直接剪枝
            out.append((self._row_to_mem(row), float(sims[i])))
        return out

    # -- 治理 ---------------------------------------------------------------

    # ------------------------------------------------------------------
    # G1：语义向量索引（vectors_sem，provider/dim 与 hash 分离，2026-09-06）
    # ------------------------------------------------------------------

    def add_semantic(self, mem_id: str, scope: str, embedding: list[float]) -> None:
        """写入/覆盖一条语义向量（幂等 upsert）。"""
        arr = np.asarray(embedding, dtype=np.float32)
        with self._lock:
            self._conn.execute(
                "INSERT INTO vectors_sem(mem_id, scope, dim, embedding) VALUES(?,?,?,?) "
                "ON CONFLICT(mem_id) DO UPDATE SET scope=excluded.scope, dim=excluded.dim, "
                "embedding=excluded.embedding",
                (mem_id, scope, int(arr.size), arr.tobytes()),
            )
            self._conn.commit()

    def vector_dim(self, scope: str) -> int | None:
        """hash 向量通道可用性：vectors 表有行即返回本 store 维度（防御 legacy 混维库：
        真实库曾混入 1024 行——缓存按 self._dim 过滤，查询须用同维 hash 嵌入）。"""
        row = self._read_connection().execute("SELECT COUNT(*) FROM vectors").fetchone()
        return self._dim if row and int(row[0]) > 0 else None

    def sem_count(self, scope: str) -> int:
        """scope 的语义向量条数（>0 才走语义通道，否则静默回退）。"""
        row = self._read_connection().execute(
            "SELECT COUNT(*) FROM vectors_sem WHERE scope = ?", (scope,)
        ).fetchone()
        return int(row[0]) if row else 0

    def sem_search(
        self, embedding: list[float], scope: str, limit: int, min_cos: float = 0.05
    ) -> list[tuple[MemoryInstance, float]]:
        """语义通道（独立表）：纯余弦降序；RULE/no_decay 豁免下限（与 vector_search 同则）。"""
        conn = self._read_connection()
        rows = conn.execute(
            "SELECT mem_id, dim, embedding FROM vectors_sem WHERE scope = ?", (scope,)
        ).fetchall()
        if not rows:
            return []
        q = np.asarray(embedding, dtype=np.float32)
        ids: list[str] = []
        mat: list[np.ndarray] = []
        for r in rows:
            dim = int(r["dim"])
            if dim != q.size:
                continue  # 维度不一致（provider 切换残留）跳过该行
            ids.append(r["mem_id"])
            mat.append(np.frombuffer(r["embedding"], dtype=np.float32).copy())
        if not ids:
            return []
        M = np.stack(mat)
        qn = np.linalg.norm(q)
        denom = np.clip(np.linalg.norm(M, axis=1) * qn, 1e-9, None)
        sims = (M @ q) / denom
        order = np.argsort(-sims)[:limit]
        out: list[tuple[MemoryInstance, float]] = []
        for i in order:
            row = conn.execute(
                "SELECT mem_type, no_decay FROM memories WHERE id = ? AND status = 'ACTIVE'",
                (ids[i],),
            ).fetchone()
            if row is None:
                continue
            is_rule = row["mem_type"] == MemoryType.RULE.value or bool(row["no_decay"])
            if (not is_rule) and float(sims[i]) < min_cos:
                continue
            full = conn.execute(
                "SELECT * FROM memories WHERE id = ? AND status = 'ACTIVE'", (ids[i],)
            ).fetchone()
            if full is None:
                continue
            out.append((self._row_to_mem(full), float(sims[i])))
        return out

    def supersede(self, old_id: str, new_id: str, scope: str) -> None:
        """矛盾覆盖（业务文档 8.5）：旧记忆标记过期但保留可追溯（维度 C）。

        允许作用于 RULE —— 规则可被新规则取代，但不可静默衰减遗忘。
        """
        with self._lock:
            now_ms = int(time.time() * 1000)
            cur = self._conn.execute(
                "UPDATE memories SET status = ?, valid_to_ms = ?, superseded_by = ? "
                "WHERE id = ? AND scope = ? AND status = 'ACTIVE'",
                (MemoryStatus.SUPERSEDED.value, now_ms, new_id, old_id, scope),
            )
            self._conn.commit()
            if cur.rowcount:
                self._active_set_version += 1  # ACTIVE 成员集变更：时间线必须全量重建
            if cur.rowcount == 0:
                raise KeyError(f"active memory {old_id} not found in scope {scope}")

    def forget(self, mem_id: str, scope: str) -> None:
        """衰减遗忘入口。RULE/no_decay 记忆 100% 拒触（维度 F 红线不变量）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT mem_type, no_decay FROM memories WHERE id = ? AND scope = ?",
                (mem_id, scope),
            ).fetchone()
            if row is None:
                raise KeyError(f"memory {mem_id} not found in scope {scope}")
            if row["mem_type"] == MemoryType.RULE.value or row["no_decay"]:
                raise RuleImmortalError(
                    f"memory {mem_id} is RULE/no_decay and must never be forgotten"
                )
            self._conn.execute(
                "UPDATE memories SET status = ? WHERE id = ? AND scope = ?",
                (MemoryStatus.FORGOTTEN.value, mem_id, scope),
            )
            self._conn.commit()
            self._active_set_version += 1  # ACTIVE 成员集变更：时间线必须全量重建

    @property
    def connection(self) -> sqlite3.Connection:
        """写连接（同包 TaskJournal/图模块复用，共享同一锁/事务上下文，避免多连接写冲突）。

        只能在 `store.lock` 内用于写入；纯 SELECT 请改用 `store.read_connection`。
        """
        return self._conn

    @property
    def read_connection(self) -> _ReadConn:
        """本线程只读连接（query_only）：供模块级裸 SELECT 使用，不涉写锁也不参写事务。"""
        return self._read_connection()

    @property
    def lock(self) -> threading.RLock:
        """写锁（门面/图模块直操 connection 时必须同源持锁；读侧不需它）。"""
        return self._lock

    def close(self) -> None:
        """关闭底层连接（进程内门面/脚本用完释放；服务端在 lifespan 调用）。

        返回后不得再有任何连接挂着这个库文件：后台线程（如异步增强 worker）可能
        正持着只读连接，留着它就会钉住库文件。回收只能走语句间隙（见 _ReadConn）：
        本线程的直接关，别的线程的限时等到间隙再关；等不到就留给它自己退场——
        跳线程硬关会 access violation（实测），那比一个残留句柄严重得多。
        """
        me = threading.get_ident()
        with self._lock:
            self._conn.close()
            with self._cache_lock:
                self._closed = True
                keep: list[tuple[int, _ReadConn]] = []
                for ident, rc in self._read_cons:
                    if ident == me:
                        rc.release(0.0)  # 本线程建的连接没有并发，间隙必定可得
                        continue
                    if not rc.release(0.2):
                        keep.append((ident, rc))
                self._read_cons = keep
            if getattr(self._read_local, "con", None) is not None:
                self._read_local.con = None

    def touch_retrieval(self, mem_ids: list[str], scope: str) -> None:
        """检索命中即强化（测试效应，Science 2008 / 论证文档 2026-08-31 第三节）：

        命中记忆的 FSRS last_review_at 重置、巩固等级 +1、stability 按成功检索增长
        （P1-6：最小稳定性更新，兑现「用得越多越牢」——S' = S × FSRS_S_GAIN，
        封顶 FSRS_S_CAP_DAYS 防失控）。RULE/no_decay 同样更新——巩固只增不减，
        与衰减调制无关；遗忘/失败驱动的 S 下调与遗忘引擎建模见 docs/research 论证。
        """
        with self._lock:
            now = time.time()
            if not mem_ids:
                return
            # P1-1：批量单查询（IN）→ 单事务批量 UPDATE，替代逐条 SELECT+UPDATE，
            # Search 热路径时延不再随命中数线性涨（commit 从 N 次降为 1 次…保持 1 次）
            placeholders = ",".join("?" for _ in mem_ids)
            rows = self._conn.execute(
                f"SELECT id, fsrs_json FROM memories WHERE scope = ? AND id IN ({placeholders})",
                [scope, *mem_ids],
            ).fetchall()
            updates: list[tuple[str, str, str]] = []
            for row in rows:
                fsrs = FSRSState(**json.loads(row["fsrs_json"]))
                fsrs.last_review_at = now
                fsrs.consolidation_level += 1
                fsrs.stability = min(fsrs.stability * FSRS_S_GAIN, FSRS_S_CAP_DAYS)
                updates.append((fsrs.model_dump_json(), row["id"], scope))
            self._conn.executemany(
                "UPDATE memories SET fsrs_json = ? WHERE id = ? AND scope = ?",
                updates,
            )
            self._conn.commit()

    def add_links(self, mem_id: str, scope: str, link_target_ids: list[str]) -> int:
        """检索增强重编码写回：给记忆追加关联（簇间桥梁，维度 B）。

        幂等去重：同一关联不重复加。返回新增关联数。
        RARE 语义：同一次检索命中的记忆两两关联，关联证据 = 用户真实查询。
        """
        if not link_target_ids:
            return 0
        added = 0
        with self._lock:
            row = self._conn.execute(
                "SELECT links_json FROM memories WHERE id = ? AND scope = ?", (mem_id, scope)
            ).fetchone()
            if row is None:
                return 0
            existing = set(json.loads(row["links_json"]) if row["links_json"] else [])
            new_ids = [i for i in link_target_ids if i not in existing and i != mem_id]
            if new_ids:
                merged = list(existing) + new_ids
                self._conn.execute(
                    "UPDATE memories SET links_json = ? WHERE id = ? AND scope = ?",
                    (json.dumps(merged), mem_id, scope),
                )
                added = len(new_ids)
            self._conn.commit()
        return added

    def clear_scope(self, scope: str) -> int:
        """赛后数据清理（官方：评测数据 30 天内删除）。返回删除条数。

        journal 同步清理：app 模式流水存 query/记忆原文，属评测用户数据的一部分，
        DELETE /scope 后仍留库即违反"评测数据不留日志"红线。
        """
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE scope = ?", (scope,)
            ).fetchone()[0]
            self._conn.execute("DELETE FROM memories WHERE scope = ?", (scope,))
            self._conn.execute("DELETE FROM memories_fts WHERE scope = ?", (scope,))
            self._conn.execute(
                "DELETE FROM vectors WHERE mem_id NOT IN (SELECT id FROM memories)"
            )
            self._conn.execute("DELETE FROM links WHERE scope = ?", (scope,))
            self._conn.execute("DELETE FROM link_types WHERE scope = ?", (scope,))
            self._conn.execute("DELETE FROM journal WHERE scope = ?", (scope,))
            self._conn.execute("DELETE FROM scope_meta WHERE scope = ?", (scope,))
            self._conn.commit()
            with self._cache_lock:  # 缓存失效与读侧快照互斥（锁序 _lock -> _cache_lock）
                self._vec_matrix = None  # 触发下次惰性重建
                self._vec_rows = 0
                self._vec_pages = []
                self._vec_ids = []
                self._vec_scopes = []
                self._scope_rows = {}
                self._scope_idx_cache.clear()
                self._timeline_cache.pop(scope, None)
                self._active_set_version += 1
            return n

    # ------------------------------------------------------------------
    # scope 元数据与 30 天 TTL（P0-3 合规：评测数据任务完成后 30 天内删除）
    # ------------------------------------------------------------------

    def record_scope(self, scope: str, source: str = "app") -> None:
        """惰性登记 scope 写入元数据（首末时间/来源）。

        source 语义：'eval'=参赛评测数据（可被 30 天 TTL 自动清扫）、
        'app'=真实使用数据（永不自动删）。已登记 app 的 scope 不会被降级。
        """
        with self._lock:
            now = int(time.time() * 1000)
            self._conn.execute(
                "INSERT INTO scope_meta(scope, first_write_ms, last_write_ms, source) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET "
                " last_write_ms=excluded.last_write_ms,"
                " source=CASE WHEN scope_meta.source='app' AND excluded.source='eval' "
                "              THEN 'eval' ELSE scope_meta.source END",
                (scope, now, now, source),
            )
            self._conn.commit()

    def sweep_expired_eval_scopes(
        self, ttl_days: int = 30, now_ms: int | None = None
    ) -> list[str]:
        """官方合规清扫：删除 source=eval 且最后一次写入距今超过 ttl 的 scope。

        真实使用（app）scope 不标记不删；返回被清扫的 scope 列表（幂等可重入）。
        """
        now = int(time.time() * 1000) if now_ms is None else now_ms
        rows = self._read_connection().execute(
            "SELECT scope FROM scope_meta "
            "WHERE source='eval' AND last_write_ms < ?",
            (now - ttl_days * 86_400_000,),
        ).fetchall()
        cleared: list[str] = []
        for (scope,) in rows:
            self.clear_scope(scope)  # 同时清理 memories/fts/vectors/links/journal/scope_meta
            cleared.append(scope)
        return cleared

    def scope_meta_rows(self, limit: int = 500) -> list[dict]:
        """只读盘点（health/自证用）：scope 元数据行。"""
        rows = self._read_connection().execute(
            "SELECT scope, first_write_ms, last_write_ms, source "
            "FROM scope_meta ORDER BY last_write_ms DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "scope": r["scope"],
                "first_write_ms": r["first_write_ms"],
                "last_write_ms": r["last_write_ms"],
                "source": r["source"],
            }
            for r in rows
        ]