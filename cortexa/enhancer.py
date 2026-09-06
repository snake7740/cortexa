"""Enhancer（双阶段 Add 的异步增强器）：对已入库记忆补充组织维度。

设计文档：docs/design/scene-aware-architecture.md（v2.1 §5.2 异步增强）

同步 Add 只做预算内核心（规则分类/编码/嵌入/入库），返回 success 后由 Enhancer
在后台补充：
1. 实体规范化（schema 已知角色匹配 → 补 entities；Cast 别名消解由 S2c 接）
2. 关系补边（schema 关系句式 → graph.links）
3. Fact 事件（schema 事件谓词 → 生命周期事件候选）

幂等：memory.metadata["enhanced"]=true 标记，已增强不重做。
失败语义：增强失败不阻塞、不改原文（content 永不改——维度 A 红线），
只补充 entities_json/links/结构化事件。评测预算：eval 模式下可由调用方关闭。
"""

from __future__ import annotations

import json
import re
import threading

from .config import Settings
from .domain.cast import CastRegistry
from .domain.facts import FactExtractor
from .llm import LLMClient
from .models import MemoryInstance
from .schema import SceneSchema, get_schema
from .storage import MemoryStore


class Enhancer:
    """对单条/多条记忆执行异步增强（确定性 + 可选 LLM 实体消歧）。"""

    def __init__(
        self,
        store: MemoryStore,
        llm: LLMClient | None,
        settings: Settings,
        schema: SceneSchema | None = None,
        entities: list[str] | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._settings = settings
        # P1-5（2026-09-06）：schema 从 settings.scene 解析（此前默认 novel，AML 场景漂移）
        self._schema = schema or get_schema(self._settings.scene)
        self._fx = FactExtractor(self._schema)
        # 已知实体（Cast/角色名单）；缺省从 schema.characters+aliases 取（场景驱动）
        if entities is not None:
            self._entities = list(entities)
        else:
            self._entities = CastRegistry(self._schema).known_names()

    # -- 幂等 ---------------------------------------------------------------
    # 增强操作本身幂等：enrich_entities 去重（已存在实体不重复加）；
    # 关系边由 graph.add_link + link_types 主键去重。无需 enhanced 标记列。
    _enhanced_ids: set[str] = set()  # 进程内防重放（MVP；跨进程天然幂等）

    # -- 实体补全 -----------------------------------------------------------

    def enrich_entities(self, mem: MemoryInstance) -> list[str]:
        """从 schema 已知角色补全 entities（不发明新实体——防幻觉）。"""
        present = [e for e in self._entities if e in mem.content]
        existing = set(mem.entities or [])
        for e in present:
            existing.add(e)
        return sorted(existing)

    # -- 关系补边 -----------------------------------------------------------

    def infer_relations(self, mem: MemoryInstance, chapter: int | None) -> list[dict]:
        """用 schema 关系句式找显式关系（A/B 必须是已知实体）。"""
        found: list[dict] = []
        if not self._entities or chapter is None:
            return found
        known_set = set(self._entities)
        for sent in re.split(r"[。！？!?\n]+", mem.content):
            for pat, rtype in self._schema.relation_patterns:
                m = re.search(pat, sent)
                if not m:
                    continue
                a = m.group("A").strip()
                b = m.group("B").strip()
                if a in known_set and b in known_set and a != b:
                    found.append({
                        "src": a, "dst": b, "link_type": rtype,
                        "evidence": sent.strip()[:120],
                        "evidence_chapter": chapter,
                    })
                break
        return found

    # -- 单条增强入口 -------------------------------------------------------

    def enhance(self, mem: MemoryInstance, scope: str) -> dict:
        """对单条记忆执行增强。返回统计。幂等（同进程已增强跳过）。"""
        if mem.id in self._enhanced_ids:
            return {"skipped": True, "reason": "already enhanced"}
        result: dict = {"entities_added": 0, "relations": 0}

        # 1. 实体补全
        new_entities = self.enrich_entities(mem)
        if new_entities and sorted(new_entities) != sorted(mem.entities or []):
            with self._store.lock:
                self._store.connection.execute(
                    "UPDATE memories SET entities_json = ? WHERE id = ? AND scope = ?",
                    (json.dumps(new_entities, ensure_ascii=False), mem.id, scope),
                )
                self._store.connection.commit()
            result["entities_added"] = len(new_entities) - len(mem.entities or [])

        # 2. 关系补边（仅当 entities 非空且内容含关系句式）
        chapter = None
        import re as _re
        m = _re.search(r"ch(\d+)", mem.session_id or "", _re.IGNORECASE)
        if m:
            chapter = int(m.group(1))
        rels = self.infer_relations(mem, chapter)
        if rels:
            from .modules import graph as _graph
            for rel in rels:
                try:
                    with self._store.lock:
                        _graph.declare_link_type(
                            self._store.connection, scope, rel["link_type"], "m-m"
                        )
                        _graph.add_link(
                            self._store.connection, scope,
                            rel["src"], rel["dst"], rel["link_type"],
                            evidence=rel["evidence"],
                            evidence_chapter=rel["evidence_chapter"],
                        )
                        result["relations"] += 1
                except Exception as _exc:  # noqa: BLE001 —— 建边失败不阻断，进观察面
                    from .enhance_stats import stats
                    stats.inc("graph", err=_exc)

        self._enhanced_ids.add(mem.id)
        return result
# ---------------------------------------------------------------------------
# 增强线程注册与优雅收尾（P1-5，2026-09-06）
# ---------------------------------------------------------------------------

_ACTIVE: list[threading.Thread] = []
_ACTIVE_LOCK = threading.Lock()


def track_enhance_thread(t: threading.Thread) -> None:
    """登记一个增强后台线程（服务/门面关闭时 drain，避免进程退出丢结果）。"""
    with _ACTIVE_LOCK:
        _ACTIVE.append(t)


def wait_enhance_threads(timeout_s: float = 3.0) -> int:
    """等待已登记的增强线程收尾；返回仍存活的线程数（超时未完成的明确丢弃并计数）。"""
    with _ACTIVE_LOCK:
        pending = list(_ACTIVE)
        _ACTIVE.clear()
    for t in pending:
        t.join(timeout=timeout_s / max(len(pending), 1))
    return sum(1 for t in pending if t.is_alive())
