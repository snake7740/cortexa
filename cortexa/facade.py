"""进程内门面（Façade）：把 Cortexa 作为类库嵌入任意 Python 应用。

设计决策（ADR-0005 双入口单管线）：HTTP 服务(server.py /add /search)与本门面共用
同一条管线——Add 走 modules/pipeline.run_add，Search 走 modules/search.search_memory，
存储与配置完全一致。真实使用（小说文本/代码开发/学术推理）产生的反馈
（规则识别误报、召回质量、排序问题）对参赛管线同等有效，反馈不失真。

用法：
    from cortexa import Cortexa

    with Cortexa() as cm:  # 默认数据目录 ~/.cortexa
        cm.remember("这个项目的包管理器统一用 pnpm，不要用 npm", scope="my-project")
        print(cm.recall_texts("包管理器用什么", scope="my-project"))
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .journal import TaskJournal
from .llm import LLMClient
from .models import AddRequest, AddResponse, AmlMessage, content_hash, SearchResult
from .modules import graph as _graph
from .modules.consistency import (
    AuditReport,
    audit as run_audit,
    build_state_line,
    DEFAULT_SCALE,
)
from .modules.pipeline import run_add
from .modules.checkup import CheckupReport, run_checkup
from .modules.search import search_memory
from .storage import SQLiteMemoryStore

_CH_FROM_SESSION = re.compile(r"ch(\d+)", re.IGNORECASE)


def _read_plain_env(path: Path) -> dict[str, str]:
    """读 env 文件中的无前后缀 KEY=VALUE（注释/空行跳过）。

    pydantic-settings 只认 CORTEXA_ 前缀变量；生态习惯的 OPENAI_API_KEY 等
    写在配置文件里同样应当生效，故在兑底层手工解析。
    """
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


class Cortexa:
    """进程内记忆系统：remember / recall / stats / forget_scope。"""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        config_file: str | Path | None = None,
        embedding_provider: str | None = None,
        openai_api_key: str | None = None,
        openai_base_url: str | None = None,
        embedding_dim: int | None = None,
    ) -> None:
        """data_dir 缺省 ~/.cortexa（类库不污染调用方工作目录）。

        端点配置（任意 OpenAI 兼容云端：DeepSeek/通义/Kimi/中转/本地代理）：
        1) config_file 显式指定 env 格式配置文件；缺省自动探测
           data_dir/.env → cwd/.env（见仓库根 .env.example 模板）；
        2) 无前后缀环境变量 OPENAI_API_KEY / OPENAI_BASE_URL 兼容兑底；
        3) 显式构造参数优先于一切。
        传 openai_api_key 且未显式指定 embedding_provider 时自动切换 openai 嵌入
        （语义召回质量显著高于离线 hash，中文跨词汇检索尤其明显）。
        """
        overrides: dict = {}
        _cfg = Path(config_file) if config_file else None
        if _cfg is None:
            _data = Path(data_dir) if data_dir is not None else Path.home() / ".cortexa"
            for cand in (_data / ".env", Path(".env")):
                if cand.exists():
                    _cfg = cand
                    break
        overrides["_env_file"] = str(_cfg) if _cfg else None
        overrides["data_dir"] = (
            Path(data_dir) if data_dir is not None else Path.home() / ".cortexa"
        )
        if embedding_provider is not None:
            overrides["embedding_provider"] = embedding_provider
        elif openai_api_key:
            overrides["embedding_provider"] = "openai"
        if embedding_dim is not None:
            overrides["embedding_dim"] = embedding_dim
        if openai_api_key is not None:
            overrides["openai_api_key"] = openai_api_key
        if openai_base_url is not None:
            overrides["openai_base_url"] = openai_base_url

        self._settings = Settings(**overrides)
        # 配置文件内的无前后缀变量（用户习惯写法），低于 CORTEXA_ 前缀、高于机器环境变量
        if _cfg is not None:
            _plain = _read_plain_env(_cfg)
            if not self._settings.openai_api_key and _plain.get("OPENAI_API_KEY"):
                self._settings.openai_api_key = _plain["OPENAI_API_KEY"]
            if not self._settings.openai_base_url and _plain.get("OPENAI_BASE_URL"):
                self._settings.openai_base_url = _plain["OPENAI_BASE_URL"]
        # 生态约定俗成的无前后缀环境变量兑底（.env/构造参数已给值则不覆盖）
        if not self._settings.openai_api_key:
            self._settings.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
        if not self._settings.openai_base_url:
            self._settings.openai_base_url = os.environ.get("OPENAI_BASE_URL", "")
        if (
            embedding_provider is None
            and self._settings.embedding_provider == "hash"
            and self._settings.openai_api_key
        ):
            self._settings.embedding_provider = "openai"
        self._settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._store = SQLiteMemoryStore(
            db_path=str(self._settings.data_dir / "cortexa.db"),
            embedding_dim=self._settings.embedding_dim,
        )
        self._llm = LLMClient(self._settings)
        self._journal = TaskJournal(
            self._store.connection, mode=self._settings.journal_mode
        )

    # 任务流水：remember/recall 的过程留痕与孤岛统计（小说线索一致性、
    # 代码决策账本的审计层；eval 模式自动脱敏合规官方"评测数据不留日志"）
    @property
    def journal(self) -> TaskJournal:
        return self._journal

    # -- 写入 ---------------------------------------------------------------

    def remember(
        self,
        content: str,
        *,
        scope: str,
        role: str = "user",
        timestamp: int | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> AddResponse:
        """记录单条记忆（与 AML Add 契约同构：识别->编码->维度地板->入库）。"""
        msg: dict = {"role": role, "content": content}
        if timestamp is not None:
            msg["timestamp"] = timestamp
        return self.remember_messages(
            [msg], scope=scope, session_id=session_id, request_id=request_id
        )

    def remember_messages(
        self,
        messages: list[dict],
        *,
        scope: str,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> AddResponse:
        """批量记录。dict 支持 role/content/text/timestamp/ts 等官方别名。"""
        req = AddRequest(
            request_id=request_id or f"lib-{uuid4().hex[:12]}",
            messages=[AmlMessage(**m) for m in messages],
            scope=scope,
            session_id=session_id,
        )
        return run_add(self._store, self._llm, self._settings, req, journal=self._journal)

    # -- 检索 ---------------------------------------------------------------

    def recall(self, query: str, *, scope: str, top_k: int = 5) -> list[SearchResult]:
        """官方 SearchResult（id/content/score/created_at）相关性降序数组。"""
        return search_memory(
            self._store,
            self._llm,
            self._settings,
            query=query,
            scope=scope,
            top_k=top_k,
            journal=self._journal,
        )

    def recall_texts(self, query: str, *, scope: str, top_k: int = 5) -> list[str]:
        """便捷入口：只返回记忆文本。"""
        return [r.content for r in self.recall(query, scope=scope, top_k=top_k)]

    # -- 管理 ---------------------------------------------------------------

    def stats(self, scope: str | None = None) -> dict:
        return {
            "active_memories": self._store.count(scope),
            "data_dir": str(self._settings.data_dir),
            "llm_model": self._settings.llm_model,
            "embedding_provider": self._settings.embedding_provider,
            "token_usage": self._llm.usage.total_tokens,
            "journal_mode": self._settings.journal_mode,
        }

    def forget_scope(self, scope: str) -> int:
        """清空某 scope（返回删除条数；与 HTTP DELETE /scope/{scope} 同款语义）。"""
        return self._store.clear_scope(scope)

    # -- 审计 ---------------------------------------------------------------

    def audit(
        self,
        scope: str,
        *,
        subjects: tuple[str, ...] = (),
        items: tuple[str, ...] = (),
        scale: dict[str, int] | None = None,
    ) -> AuditReport:
        """一致性审计（本地零 LLM）：实体归属状态线 + 回跳/跳级 + 物品缺口。

        subjects 是待追踪实体名单（调用方提供，如主角/关键角色）；
        items 是待追踪物品；scale 可覆盖默认修仙等级表。
        语义级冲突检测需 LLM，属提分阶段，本 API 只做结构级。
        """
        return run_audit(
            self._store.connection,
            scope,
            subjects=list(subjects),
            items=list(items),
            scale=scale,
        )

    def checkup(
        self,
        scope: str,
        *,
        subjects: tuple[str, ...] = (),
        items: tuple[str, ...] = (),
        probes: list[tuple[str, str]] | None = None,
        top_k: int = 3,
    ) -> CheckupReport:
        """全维度查体（“学习委员”）：地板复验/孤岛/衰减健康度/探针自测/一致性。

        只读，不改任何记忆；探针自测经测试效应会强化命中记忆（副作用即机制）。
        subjects/items 为空则跳过一致性审计（系统不假装能自动识别人名）。
        """
        return run_checkup(
            self._store,
            self._journal,
            self._settings,
            self._llm,
            scope,
            subjects=list(subjects) or None,
            items=list(items) or None,
            probes=probes,
            top_k=top_k,
        )

    # -- 修正写入（Palantir Action type 移植：写入类型化 + 强制取代链）-------

    def retcon(
        self,
        scope: str,
        old_content: str,
        new_content: str,
        *,
        subject: str = "",
        session_id: str | None = None,
        reason: str = "",
    ) -> dict:
        """修正已入库记忆：旧记忆转 SUPERSEDED，新记忆带 op_type='retcon'
        与 supersedes_id 强制取代链，journal 留痕（同官方 edits history）。

        新记忆继承旧记忆的 session_id 与事件时间（逻辑时间轴不漂移）；
        旧记忆不存在/已被取代时拒写（不猜对象，宁可报错不可脏写）。
        """
        old = self._store.get_by_hash(scope, content_hash(scope, old_content))
        if old is None:
            raise KeyError(f"no ACTIVE memory with given content in scope {scope!r}")
        if new_content.strip() == old_content.strip():
            raise ValueError("retcon new_content must differ from old_content")

        resp = self.remember(
            new_content,
            scope=scope,
            session_id=session_id if session_id is not None else old.session_id,
            timestamp=old.event_ts_ms,
        )
        new = self._store.get_by_hash(scope, content_hash(scope, new_content))
        if new is None or resp.stored == 0:
            raise RuntimeError("retcon new memory not stored (hash collision?)")

        self._store.supersede(old.id, new.id, scope)
        with self._store.lock:
            self._store.connection.execute(
                "UPDATE memories SET op_type = 'retcon', supersedes_id = ? WHERE id = ?",
                (old.id, new.id),
            )
            self._store.connection.commit()

        m = _CH_FROM_SESSION.search(old.session_id or "")
        self._journal.record(
            "retcon",
            scope=scope,
            session_id=old.session_id,
            subject=subject,
            old_id=old.id,
            new_id=new.id,
            chapter=int(m.group(1)) if m else None,
            reason=reason,
        )
        return {
            "old_id": old.id,
            "new_id": new.id,
            "chapter": int(m.group(1)) if m else None,
            "scope": scope,
        }

    # -- 实体关系图（Palantir Link type 移植：单表双向/类型化/基数守卫）----

    def declare_link_type(
        self, scope: str, name: str, cardinality: str = "m-m"
    ) -> None:
        """声明关系类型与基数（1-1/1-m/m-1/m-m，幂等）。未声明类型连边时自动按 m-m 登记。

        写路径统一持 store.lock：与 retcon/journal 同锁纪律，HTTP 多线程下不互踩。
        """
        with self._store.lock:
            _graph.declare_link_type(self._store.connection, scope, name, cardinality)

    def link_types(self, scope: str) -> dict[str, str]:
        """scope 内已声明的关系类型 {name: cardinality}。"""
        return _graph.link_types(self._store.connection, scope)

    def link(
        self,
        scope: str,
        src: str,
        dst: str,
        link_type: str,
        *,
        evidence: str = "",
        evidence_chapter: int | None = None,
        replace: bool = False,
    ) -> str:
        """连一条边，返回边 id。基数违例时 replace=True 取代旧边留痕，否则拒写。"""
        with self._store.lock:
            return _graph.add_link(
                self._store.connection,
                scope,
                src,
                dst,
                link_type,
                evidence=evidence,
                evidence_chapter=evidence_chapter,
                replace=replace,
            )

    def unlink(self, scope: str, link_id: str) -> bool:
        """删边（置 REMOVED 留痕），返回是否命中活跃边。"""
        with self._store.lock:
            return _graph.remove_link(self._store.connection, scope, link_id)

    def neighbors(
        self,
        scope: str,
        entity: str,
        *,
        link_type: str | None = None,
        include_history: bool = False,
    ) -> list[tuple[str, _graph.LinkRow]]:
        """双向遍历：实体的活跃关系 (对端名, 边) 列表。"""
        return _graph.neighbors(
            self._store.connection,
            scope,
            entity,
            link_type=link_type,
            include_history=include_history,
        )

    def link_history(self, scope: str, entity: str) -> list[_graph.LinkRow]:
        """实体全部边史（含被取代/删除，编辑史可追溯）。"""
        return _graph.link_history(self._store.connection, scope, entity)

    # -- 实体编辑史 ---------------------------------------------------------

    def entity_history(self, scope: str, subject: str, scale: dict[str, int] | None = None) -> dict:
        """单实体编辑史：状态线 + retcon 修正事件 + 边史（按章排序）。

        三源合流回答"这个实体我们记录过什么、改过什么、和谁连过边"——
        对应 Palantir 的 edits history 与对象视图。
        """
        conn = self._store.connection
        rows = conn.execute(
            "SELECT id, session_id, content FROM memories "
            "WHERE scope = ? AND status = 'ACTIVE' "
            "AND (session_id LIKE 'ch%' OR content LIKE '【第%章%')",
            (scope,),
        ).fetchall()
        memories = [(r["id"], r["session_id"], r["content"]) for r in rows]
        state_line = build_state_line(memories, subject, scale or DEFAULT_SCALE)
        retcons = [
            {
                "ts_ms": e.ts_ms,
                "chapter": e.detail.get("chapter"),
                "old_id": e.detail.get("old_id"),
                "new_id": e.detail.get("new_id"),
                "reason": e.detail.get("reason", ""),
            }
            for e in self._journal.events("retcon", scope=scope, limit=None)  # 全量：限额会丢早章修正史
            if e.detail.get("subject") == subject
        ]
        return {
            "subject": subject,
            "state_line": [
                {
                    "chapter": p.chapter,
                    "value": p.value,
                    "confidence": p.confidence,
                    "evidence": p.evidence,
                    "mem_id": p.mem_id,
                }
                for p in state_line
            ],
            "retcons": sorted(retcons, key=lambda r: (r["chapter"] or 0, r["ts_ms"])),
            "links": [
                {
                    "src": l.src,
                    "dst": l.dst,
                    "link_type": l.link_type,
                    "status": l.status,
                    "evidence": l.evidence,
                    "evidence_chapter": l.evidence_chapter,
                }
                for l in _graph.link_history(conn, scope, subject)
            ],
        }

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "Cortexa":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
