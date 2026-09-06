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


class SQLiteMemoryStore(MemoryStore):
    """MVP 嵌入式实现：SQLite + FTS5 + 内存 numpy 向量余弦。"""

    # 向量缓存页大小（PagedAttention 思想）：追加只写页槽，消除逐条 vstack 拷贝
    _VEC_PAGE_SIZE = 256

    def __init__(self, db_path: str, embedding_dim: int = 512) -> None:
        self._dim = embedding_dim
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        init_graph_schema(self._conn)  # 实体关系图表（Palantir link type 纪律）
        # P0-3：scope 元数据表（首末写入时间/来源），30 天 TTL 合规清扫的依据
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS scope_meta ("
            " scope TEXT PRIMARY KEY,"
            " first_write_ms INTEGER,"
            " last_write_ms INTEGER,"
            " source TEXT NOT NULL DEFAULT 'app')"
        )
        self._migrate_memories_columns()  # 既有库补列（op_type/supersedes_id）
        self._conn.commit()
        # 向量内存缓存（懒加载，写入时同步追加）；_vec_pages 是分页待合并区，
        # _vec_matrix 非 None 表示已冻结的全量矩阵（页已全部折入）
        self._vec_ids: list[str] = []
        self._vec_scopes: list[str] = []
        self._vec_pages: list[list[np.ndarray]] = []
        self._vec_matrix: np.ndarray | None = None

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
        vec = np.asarray(embedding, dtype=np.float32)
        if self._vec_matrix is not None:
            # 已冻结（检索发生过）后的追加：逐条 vstack。同步小批量写入下
            # 单次拷贝毫秒级可接受；分页只消除冷启动大批量摄取的 O(N²)
            self._vec_matrix = np.vstack([self._vec_matrix, vec])
            self._vec_ids.append(mem_id)
            self._vec_scopes.append(scope)
            return
        page_size = self._VEC_PAGE_SIZE
        if not self._vec_pages or len(self._vec_pages[-1]) >= page_size:
            self._vec_pages.append([vec])
        else:
            self._vec_pages[-1].append(vec)
        self._vec_ids.append(mem_id)
        self._vec_scopes.append(scope)

    def _ensure_vec_cache(self) -> np.ndarray:
        """首次向量检索前把分页折成单一矩阵（此后追加走 vstack 兑底）。"""
        if self._vec_matrix is not None:
            if len(self._vec_ids) > self._vec_matrix.shape[0]:
                # 防御：冻结后若有追加致 ids/矩阵不齐，按 ids 长度截齐（多余行无效）
                self._vec_matrix = self._vec_matrix[: len(self._vec_ids)]
            return self._vec_matrix
        if not self._vec_pages:
            # 冷启动：从库中全量载入（向量以 float32 落库，行格式同 numpy 内存布局）
            rows = self._conn.execute(
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
        if len(self._vec_ids) > self._vec_matrix.shape[0]:
            self._vec_ids = self._vec_ids[: self._vec_matrix.shape[0]]
            self._vec_scopes = self._vec_scopes[: self._vec_matrix.shape[0]]
        return self._vec_matrix

    # -- 查询 ---------------------------------------------------------------

    def get(self, mem_id: str, scope: str) -> MemoryInstance | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ? AND scope = ?", (mem_id, scope)
            ).fetchone()
            return self._row_to_mem(row) if row else None

    def get_by_hash(self, scope: str, content_hash: str) -> MemoryInstance | None:
        """按去重指纹定位活跃记忆（retcon 修正链的旧记忆定位入口）。

        UNIQUE(scope, content_hash) 保证至多一条；只看 ACTIVE——
        已被取代/归档的记忆不可再作为修正对象（避免套套链）。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE scope = ? AND content_hash = ? "
                "AND status = 'ACTIVE'",
                (scope, content_hash),
            ).fetchone()
            return self._row_to_mem(row) if row else None

    def count(self, scope: str | None = None) -> int:
        with self._lock:
            if scope is None:
                return self._conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE status = 'ACTIVE'"
                ).fetchone()[0]
            return self._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE scope = ? AND status = 'ACTIVE'",
                (scope,),
            ).fetchone()[0]

    def entity_search(self, query: str, scope: str, limit: int) -> list[tuple[MemoryInstance, float]]:
        """特征访问通道（维度 B 多跳的廉价前哨）：查询提到已知实体 → 直取其记忆。

        人类记忆类比：存储一直在，缺的是访问路径——正确提到某人/某物的特征
        即可唤起（无需整段语义相似）。实现为精确包含匹配（零幻觉），非相似度。
        千级规模扫描 ACTIVE 行即可；规模触发（>万条）后转独立倒排表，登记 P2。
        """
        q = query.casefold()
        with self._lock:
            rows = self._conn.execute(
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
        with self._lock:
            rows = self._conn.execute(
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
        with self._lock:
            rows = self._conn.execute(
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
        with self._lock:
            matrix = self._ensure_vec_cache()
            if self._vec_ids is None or matrix.shape[0] == 0:
                return []
            q = np.asarray(embedding, dtype=np.float32)
            scope_arr = np.asarray(self._vec_scopes)
            mask = scope_arr == scope
            if not mask.any():
                return []
            idx = np.nonzero(mask)[0]
            sub = matrix[idx]
            qn = np.linalg.norm(q)
            subn = np.linalg.norm(sub, axis=1)
            denom = np.clip(subn * qn, 1e-9, None)
            sims = (sub @ q) / denom
            order = np.argsort(-sims)[:limit]
            out: list[tuple[MemoryInstance, float]] = []
            for i in order:
                # RULE/no_decay 豁免语义剪枝（维度 F 红线：规则召回优先——规则是稳定
                # 约束，价值高于噪声风险；rule_boost 在融合层只加成排序不扩大召回）
                mem_id = self._vec_ids[idx[i]]
                row = self._conn.execute(
                    "SELECT mem_type, no_decay FROM memories WHERE id = ? AND status = 'ACTIVE'",
                    (mem_id,),
                ).fetchone()
                if row is None:
                    continue
                is_rule = row["mem_type"] == MemoryType.RULE.value or bool(row["no_decay"])
                if (not is_rule) and float(sims[i]) < min_cos:
                    continue  # precision-first：普通记忆低于语义下限直接剪枝
                full = self._conn.execute(
                    "SELECT * FROM memories WHERE id = ? AND status = 'ACTIVE'", (mem_id,)
                ).fetchone()
                if full is None:
                    continue
                mem = self._row_to_mem(full)
                out.append((mem, float(sims[i])))
            return out

    # -- 治理 ---------------------------------------------------------------

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

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（同包 TaskJournal 复用，共享同一锁/事务上下文，避免多连接写冲突）。"""
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        """写锁（门面/图模块直操 connection 时必须同源持锁）。"""
        return self._lock

    def close(self) -> None:
        """关闭底层连接（进程内门面/脚本用完释放；服务端在 lifespan 调用）。"""
        with self._lock:
            self._conn.close()

    def touch_retrieval(self, mem_ids: list[str], scope: str) -> None:
        """检索命中即强化（测试效应，Science 2008 / 论证文档 2026-08-31 第三节）：

        命中记忆的 FSRS last_review_at 重置、巩固等级 +1、stability 按成功检索增长
        （P1-6：最小稳定性更新，兑现「用得越多越牢」——S' = S × FSRS_S_GAIN，
        封顶 FSRS_S_CAP_DAYS 防失控）。RULE/no_decay 同样更新——巩固只增不减，
        与衰减调制无关；遗忘/失败驱动的 S 下调与遗忘引擎建模见 docs/research 论证。
        """
        with self._lock:
            now = time.time()
            for mid in mem_ids:
                row = self._conn.execute(
                    "SELECT fsrs_json FROM memories WHERE id = ? AND scope = ?", (mid, scope)
                ).fetchone()
                if row is None:
                    continue
                fsrs = FSRSState(**json.loads(row["fsrs_json"]))
                fsrs.last_review_at = now
                fsrs.consolidation_level += 1
                fsrs.stability = min(fsrs.stability * FSRS_S_GAIN, FSRS_S_CAP_DAYS)
                self._conn.execute(
                    "UPDATE memories SET fsrs_json = ? WHERE id = ? AND scope = ?",
                    (fsrs.model_dump_json(), mid, scope),
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
            self._vec_matrix = None  # 触发下次惰性重建
            self._vec_pages = []
            self._vec_ids = []
            self._vec_scopes = []
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
        rows = self._conn.execute(
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
        rows = self._conn.execute(
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