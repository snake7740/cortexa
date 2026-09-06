"""Add 同步写入管线（官方硬规则：返回成功前消息必须全部存好且可被 Search）。

管线四步：识别分流(rule_wing) -> 编码标注(encoding) -> 维度地板(dimension_floor)
-> 批量向量化 -> 入库。HTTP 端点(server.py /add)与进程内门面(facade.py)共用本函数——
双入口单管线（ADR-0005）：真实使用（小说/代码/学术 dogfooding）产生的反馈对
参赛管线同等有效。
"""

from __future__ import annotations

import re as _re

from ..config import Settings
from ..journal import TaskJournal
from ..llm import LLMClient
from ..models import AddRequest, AddResponse, MemoryInstance
from ..storage import MemoryStore
from .dimension_floor import assert_floor, enforce
from .encoding import encode_message
from .rule_wing import classify_message


def run_add(
    store: MemoryStore,
    llm: LLMClient,
    settings: Settings,
    req: AddRequest,
    journal: TaskJournal | None = None,
) -> AddResponse:
    """对请求内每条消息同步完成 去重指纹 -> 识别 -> 编码 -> 审计 -> 向量化 -> 入库。

    全程同步、无后台摄取（官方：Add 响应返回前消息必须可被 Search）。
    """
    stored = 0
    duplicates = 0
    texts: list[str] = []
    pending: list[MemoryInstance] = []
    for msg in req.messages:
        if not msg.content.strip():
            continue
        cls = classify_message(msg.content, llm, allow_llm=settings.rule_llm_fallback)
        mem = encode_message(
            msg,
            scope=req.scope,
            session_id=req.session_id,
            request_id=req.request_id,
            cls=cls,
        )
        enforce(mem)
        assert_floor(mem)  # 红线：维度地板 100% 覆盖，不合格即拒写
        texts.append(msg.content)
        pending.append(mem)

    if pending:
        vectors = llm.embed(texts)
        for mem, vec in zip(pending, vectors):
            if store.add(mem, embedding=vec):
                stored += 1
            else:
                duplicates += 1

    # ---- 写入期治理（AML 维度 D）：同主体新旧冲突触发 supersede ----
    if settings.governance_enabled and stored > 0:
        try:
            from ..domain.governance import GovernanceEngine
            from ..schema import get_schema as _get_schema
            _gov = GovernanceEngine(store, _get_schema(settings.scene))
            for m in pending:
                _stored_m = store.get(m.id, req.scope)
                if _stored_m is not None:
                    _gov.govern(_stored_m, req.scope)
        except Exception as _exc:  # noqa: BLE001 —— 治理失败不阻断写入，但进观察面
            from ..enhance_stats import stats
            stats.inc("governance", err=_exc)

    # ---- 合规登记：scope 元数据（P0-3：30 天 TTL 清扫依据）----
    # eval 部署（参赛评测）source=eval 的 scope 才会被启动清扫删除；真实使用 app 不删。
    if req.scope:
        store.record_scope(
            req.scope,
            source="eval" if settings.journal_mode == "eval" else "app",
        )

    if journal is not None:
        journal.record(
            "add",
            scope=req.scope,
            session_id=req.session_id,
            request_id=req.request_id,
            stored=stored,
            duplicates=duplicates,
            types=[m.mem_type.value for m in pending],  # 识别分流结果可回放
        )

    # ---- 领域适配器 post_ingest hook ----
    if settings.novel_adapter_enabled and pending:
        from .novel_adapter import NovelAdapter
        adapter = NovelAdapter(store, llm, settings)
        _ch_match = _re.match(r"ch(\d+)", req.session_id or "", _re.IGNORECASE)
        _chapter = int(_ch_match.group(1)) if _ch_match else None
        _full_text = "\n".join(m.content for m in pending)

        # 1. 实体消歧（可选替换启发式结果）
        _resolved = []
        if settings.adapter_entity_resolver:
            _resolved = adapter.resolve_entities(_full_text, _chapter, req.scope)
            if _resolved:
                # 用消歧结果更新已入库记忆的 entities 字段
                _new_ents = [e.name for e in _resolved if e.confidence >= 0.6]
                if _new_ents:
                    for mem in pending:
                        with store.lock:
                            import json as _json
                            store.connection.execute(
                                "UPDATE memories SET entities_json = ? WHERE id = ? AND scope = ?",
                                (_json.dumps(_new_ents, ensure_ascii=False), mem.id, req.scope),
                            )
                        store.connection.commit()

        # 2. 注册新实体 + 角色生命周期追踪
        if settings.adapter_character_tracker:
            if _resolved:
                adapter.register_new_entities(_resolved, _chapter, req.scope)
            adapter.track_characters(pending, _chapter, req.scope)

        # 3. 文笔特征（零LLM）
        if settings.adapter_style_profiler and _chapter is not None:
            adapter.profile_style(_full_text, _chapter, req.scope)

        # 4. 关系自动建边（RelationInferrer，默认关闭需显式开启）
        if settings.relation_inferrer and _chapter is not None:
            from ..models import EntityType as _ET
            from . import graph as _graph
            # 已知实体 = 已注册的 CHARACTER（不发明实体，防幻觉）
            _known = [
                r[0] for r in store.connection.execute(
                    "SELECT entity_name FROM entity_states "
                    "WHERE scope = ? AND entity_type = ?",
                    (req.scope, _ET.CHARACTER.value),
                ).fetchall()
            ]
            for rel in adapter.infer_relations(_full_text, _known, _chapter, req.scope):
                try:
                    with store.lock:
                        _graph.declare_link_type(
                            store.connection, req.scope, rel["link_type"], "m-m"
                        )
                        _graph.add_link(
                            store.connection, req.scope,
                            rel["src"], rel["dst"], rel["link_type"],
                            evidence=rel["evidence"],
                            evidence_chapter=rel["evidence_chapter"],
                        )
                except Exception as _exc:  # noqa: BLE001
                    from ..enhance_stats import stats
                    stats.inc("graph", err=_exc)

    # ---- 双阶段 Add 异步增强（方案 A：进程内 daemon 线程；不阻塞响应）----
    if settings.async_enhance_enabled and pending and stored > 0:
        try:
            import threading as _th

            def _enhance_worker():
                try:
                    from ..enhancer import Enhancer
                    # 已知实体：从本请求已存记忆的 entities 聚合（增量场景种子）
                    _seed = []
                    for m in pending:
                        for e in m.entities:
                            if e not in _seed:
                                _seed.append(e)
                    _enh = Enhancer(store, llm, settings, entities=_seed)
                    for m in pending:
                        # 已入库才有增强意义（store.add 返回 True 的）
                        _stored_m = store.get(m.id, req.scope)
                        if _stored_m is not None:
                            _enh.enhance(_stored_m, req.scope)
                except Exception as _exc:  # noqa: BLE001 —— 单条增强失败不阻塞、不重试爆炸，但进观察面
                    from ..enhance_stats import stats
                    stats.inc("enhance", err=_exc)

            _th.Thread(target=_enhance_worker, daemon=True).start()
        except Exception as _exc:  # noqa: BLE001
            from ..enhance_stats import stats
            stats.inc("enhance_spawn", err=_exc)

    return AddResponse(
        request_id=req.request_id,
        user_id=req.scope,
        session_id=req.session_id or "",
        stored=stored,
        duplicates=duplicates,
    )