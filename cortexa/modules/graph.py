"""实体关系图（Palantir Link type 建模纪律移植，2026-08-31）。

业务定位：记忆条目回答"说了什么"，关系图回答"谁和谁是什么关系"——
长篇内容（小说人物谱系/代码模块依赖）的第二层结构化记忆。

移植的三条 Palantir Ontology 建模纪律（官方 docs 核实）：
1. 单表双向：一条 link 天然支持两侧遍历，不建反向表（官方：link type
   is bidirectional, no separate reverse link type needed）；
2. link_type 必选维度：同两个实体间允许多条语义独立的边（官方例：
   Flight↔Aircraft 可同时有"执飞"与"维修记录"）——边不等于关系；
3. 基数声明 + 违例治理：1-1 / 1-m 基数下新边冲突时自动取代旧边
   （supersede 留痕，同记忆治理语义），不给替代来源则拒写
   （precision-first：宁可拒写不可脏写）。

设计边界（诚实声明）：
- 实体以名字字符串为节点（MVP 不做对象 schema，涌现友好：新实体随时可连边）；
- 语义级关系抽取（从正文自动建边）需 LLM，属提分阶段，本模块只提供
  声明式连边能力与遍历查询；
- 同 scope 内连边；跨 scope 关系不支持（同官方：跨 ontology 连边不支持）。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass

_GRAPH_SCHEMA = """
CREATE TABLE IF NOT EXISTS link_types (
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    cardinality TEXT NOT NULL,
    created_ts_ms INTEGER NOT NULL,
    PRIMARY KEY (scope, name)
);
CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    link_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    evidence TEXT NOT NULL DEFAULT '',
    evidence_chapter INTEGER,
    created_ts_ms INTEGER NOT NULL,
    removed_ts_ms INTEGER,
    superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_links_scope_src ON links (scope, src, status);
CREATE INDEX IF NOT EXISTS idx_links_scope_dst ON links (scope, dst, status);
"""

_CARDINALITIES = ("1-1", "1-m", "m-1", "m-m")


class CardinalityError(RuntimeError):
    """基数违例：受限端点已有活跃边且未提供替代来源（拒绝脏写）。"""


def init_graph_schema(conn: sqlite3.Connection) -> None:
    """建图表（幂等）。由 SQLiteMemoryStore 初始化时调用，与主 schema 同库同事务域。"""
    conn.executescript(_GRAPH_SCHEMA)


@dataclass(frozen=True)
class LinkRow:
    """一条活跃/历史边的只读视图。"""

    id: str
    src: str
    dst: str
    link_type: str
    status: str
    evidence: str
    evidence_chapter: int | None
    created_ts_ms: int
    superseded_by: str | None


def _row_to_link(row: sqlite3.Row) -> LinkRow:
    return LinkRow(
        id=row["id"],
        src=row["src"],
        dst=row["dst"],
        link_type=row["link_type"],
        status=row["status"],
        evidence=row["evidence"],
        evidence_chapter=row["evidence_chapter"],
        created_ts_ms=row["created_ts_ms"],
        superseded_by=row["superseded_by"],
    )


def declare_link_type(
    conn: sqlite3.Connection, scope: str, name: str, cardinality: str = "m-m"
) -> None:
    """声明关系类型与基数（幂等，重复声明更新基数）。

    基数中的数字 1 标记受限端（该端每个实体至多一条活跃边）：
    1-1 两侧各至多一条；1-m 仅 src 端受限；m-1 仅 dst 端受限；m-m 不限。
    例：「授业师父」声明 m-1 后 link(师父, 弟子) —— 弟子端受限（一人至多一位）。
    """
    if cardinality not in _CARDINALITIES:
        raise ValueError(f"cardinality must be one of {_CARDINALITIES}, got {cardinality!r}")
    conn.execute(
        "INSERT INTO link_types (scope, name, cardinality, created_ts_ms) VALUES (?,?,?,?) "
        "ON CONFLICT(scope, name) DO UPDATE SET cardinality = excluded.cardinality",
        (scope, name, cardinality, int(time.time() * 1000)),
    )
    conn.commit()


def link_types(conn: sqlite3.Connection, scope: str) -> dict[str, str]:
    """scope 内已声明的关系类型 {name: cardinality}。"""
    rows = conn.execute(
        "SELECT name, cardinality FROM link_types WHERE scope = ?", (scope,)
    ).fetchall()
    return {r["name"]: r["cardinality"] for r in rows}


def add_link(
    conn: sqlite3.Connection,
    scope: str,
    src: str,
    dst: str,
    link_type: str,
    *,
    evidence: str = "",
    evidence_chapter: int | None = None,
    replace: bool = False,
) -> str:
    """连一条边，返回边 id。

    基数守卫：受限端点已有活跃边时，replace=True 自动取代旧边（留痕），
    否则抛 CardinalityError。link_type 未声明时按 m-m 自动登记（涌现友好）。
    """
    if not src.strip() or not dst.strip():
        raise ValueError("src/dst entity name must not be empty")
    declared = link_types(conn, scope)
    cardinality = declared.get(link_type)
    if cardinality is None:
        declare_link_type(conn, scope, link_type, "m-m")
        cardinality = "m-m"

    # 方向归一化：m-1 时受限端在原始 dst，翻转存成 (受限端, 非受限端)，
    # 使守卫统一只查存储后的 src 端（同 1-m），四个基数一套守卫逻辑
    a, b = (dst, src) if cardinality == "m-1" else (src, dst)

    if cardinality != "m-m":
        where = ["scope = ?", "link_type = ?", "status = 'ACTIVE'"]
        params: list = [scope, link_type]
        if cardinality == "1-1":
            where.append("(src = ? OR dst = ? OR src = ? OR dst = ?)")
            params += [a, a, b, b]
        else:  # 1-m（归一化后）：src 端至多一条
            where.append("src = ?")
            params.append(a)
        conflicts = conn.execute(
            f"SELECT id FROM links WHERE {' AND '.join(where)}", params
        ).fetchall()
        if conflicts:
            if not replace:
                raise CardinalityError(
                    f"cardinality {cardinality} violated: {a} <-> {b} ({link_type}) "
                    f"conflicts with {len(conflicts)} active link(s)"
                )
            now_ms = int(time.time() * 1000)
            for c in conflicts:
                conn.execute(
                    "UPDATE links SET status = 'SUPERSEDED', superseded_by = 'pending', "
                    "removed_ts_ms = ? WHERE id = ?",
                    (now_ms, c["id"]),
                )

    link_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO links (id, scope, src, dst, link_type, status, evidence, "
        "evidence_chapter, created_ts_ms) VALUES (?,?,?,?,?,?,?,?,?)",
        (link_id, scope, a, b, link_type, "ACTIVE", evidence, evidence_chapter,
         int(time.time() * 1000)),
    )
    if cardinality != "m-m":
        conn.execute(
            "UPDATE links SET superseded_by = ? WHERE superseded_by = 'pending' AND scope = ?",
            (link_id, scope),
        )
    conn.commit()
    return link_id


def remove_link(conn: sqlite3.Connection, scope: str, link_id: str) -> bool:
    """删边（状态置 REMOVED 留痕）。返回是否命中活跃边。"""
    cur = conn.execute(
        "UPDATE links SET status = 'REMOVED', removed_ts_ms = ? "
        "WHERE id = ? AND scope = ? AND status = 'ACTIVE'",
        (int(time.time() * 1000), link_id, scope),
    )
    conn.commit()
    return cur.rowcount > 0


def neighbors(
    conn: sqlite3.Connection,
    scope: str,
    entity: str,
    *,
    link_type: str | None = None,
    include_history: bool = False,
) -> list[tuple[str, LinkRow]]:
    """双向遍历：返回 (对端实体名, 边) 列表。

    单表双向——src/dst 两侧一次查出，不建反向表。按 created_ts_ms 升序
    （关系建立先后即叙事顺序）。
    """
    status_clause = "" if include_history else " AND status = 'ACTIVE'"
    type_clause = " AND link_type = ?" if link_type else ""
    sql = (
        "SELECT * FROM links WHERE scope = ? AND (src = ? OR dst = ?)"
        f"{type_clause}{status_clause} ORDER BY created_ts_ms"
    )
    params: list = [scope, entity, entity]
    if link_type:
        params.append(link_type)
    rows = conn.execute(sql, params).fetchall()
    out: list[tuple[str, LinkRow]] = []
    for r in rows:
        other = r["dst"] if r["src"] == entity else r["src"]
        out.append((other, _row_to_link(r)))
    return out


def link_history(
    conn: sqlite3.Connection, scope: str, entity: str
) -> list[LinkRow]:
    """实体全部边史（含 SUPERSEDED/REMOVED，编辑史可追溯，同官方 edits history）。"""
    rows = conn.execute(
        "SELECT * FROM links WHERE scope = ? AND (src = ? OR dst = ?) ORDER BY created_ts_ms",
        (scope, entity, entity),
    ).fetchall()
    return [_row_to_link(r) for r in rows]
