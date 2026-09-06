"""Cortexa AML 协议服务器（FastAPI）。

端点契约（官方 api-guide 现行同步规范，字段语义见 models.py，golden 见 tests/golden）：
- POST /add    请求 request_id/messages/user_id/session_id；返回 success=true 且
               三 ID 逐字节回显；返回成功前消息必须全部存好且可被检索（同步语义）
- POST /search 请求 query/options?/user_id/top_k（正式 Top K=100）；返回 data 数组
               元素 {id, content, score?, created_at?}，按相关性降序；无命中返回空数组
- GET  /health 无需鉴权，任意 2xx 即正常（官方默认检查 Add 同源 /health）
- DELETE /scope/{scope} 运维清理（30 天 TTL 之外的人工兜底）

红线：Search 只返回证据，绝不生成最终答案（业务文档 12.3）。
端点全部为同步 def：FastAPI 自动入线程池，阻塞体（SQLite/httpx/退避 sleep）不占事件循环
（P0-2 修复：原 async def 包同步体会让单进程并发请求串行排队）。
W2 管线：/add = 识别分流(rule_wing) -> 编码标注(encoding) -> 维度地板
(dimension_floor) -> 批量向量化 -> 入库；/search = 双通道召回 + RRF 融合
+ precision-first 截断(modules/search.py)。端点只保留编排与契约转换。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from . import __version__
from .config import Settings, get_settings
from .journal import TaskJournal
from .llm import LLMClient
from .models import AddRequest, AddResponse, SearchRequest, SearchResponse
from .modules.pipeline import run_add
from .modules.search import search_memory
from .storage import SQLiteMemoryStore

logger = logging.getLogger("cortexa.server")

# ---------------------------------------------------------------------------
# 认证依赖（官方 Authentication：No authentication / Memory API Key）
# ---------------------------------------------------------------------------

def require_key(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    if not settings.api_key:
        return
    presented = x_api_key
    if presented is None and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if presented != settings.api_key:
        raise HTTPException(status_code=401, detail="invalid or missing API key")


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteMemoryStore(
        db_path=str(settings.data_dir / "cortexa.db"),
        embedding_dim=settings.embedding_dim,
    )
    llm = LLMClient(settings)
    journal = TaskJournal(store.connection, mode=settings.journal_mode)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # P0-1：启动自检——参赛部署口径不静默（真实使用按需自行设置并接受提示）
        if settings.journal_mode != "eval":
            logger.warning(
                "CORTEXA_JOURNAL_MODE=%s：参赛评测须设 eval（query/记忆原文脱敏，"
                "官方『评测数据不留日志』红线）；真实使用可保留 app。",
                settings.journal_mode,
            )
        if settings.scene != "aml":
            logger.warning(
                "CORTEXA_SCENE=%s：参赛评测须设 aml（AML 维度 D 治理按 aml schema"
                " 生效，novel schema 的中文事件词治理英文内容会空转）。",
                settings.scene,
            )
        if settings.journal_mode == "eval":
            # P0-3：合规兜底——启动即清扫超 30 天的 eval scope（任务完成后自动过期删除）
            swept = store.sweep_expired_eval_scopes()
            if swept:
                logger.info("30 天 TTL 清扫（startup）：%d 个过期 eval scope 已删除", len(swept))
        yield
        store.close()

    app = FastAPI(
        title="Cortexa Memory API",
        version=__version__,
        description="Bionic AI memory system — AML Add/Search protocol",
        lifespan=lifespan,
    )

    # 延迟观测与红线自证（业务文档 12.3 / config 红线：Search P95 与 Add 预算
    # 不能只声明不执法——/health 公开比对结果，超限即可见）
    search_latencies: deque[float] = deque(maxlen=500)
    add_latencies: deque[float] = deque(maxlen=500)

    @app.middleware("http")
    async def observe_latency(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if request.url.path == "/search":
            search_latencies.append(elapsed_ms)
            response.headers["x-cortexa-latency-ms"] = f"{elapsed_ms:.1f}"
        elif request.url.path == "/add":
            add_latencies.append(elapsed_ms)
        return response

    # -- Add ----------------------------------------------------------------

    @app.post("/add", response_model=AddResponse, dependencies=[Depends(require_key)])
    def add(req: AddRequest) -> AddResponse:
        """同步写入管线（modules/pipeline.run_add，与进程内门面共用同一实现）。"""
        return run_add(store, llm, settings, req, journal=journal)

    # -- Search -------------------------------------------------------------

    @app.post("/search", response_model=SearchResponse, dependencies=[Depends(require_key)])
    def search(req: SearchRequest) -> SearchResponse:
        """双通道召回 + RRF 融合（管线在 modules/search.py，W2 迁出）。"""
        return SearchResponse(
            data=search_memory(
                store,
                llm,
                settings,
                query=req.query,
                scope=req.scope,
                top_k=req.top_k,
                journal=journal,
            )
        )

    # -- Health / 运维 -------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        def _p95(samples: deque[float]) -> float:
            if not samples:
                return 0.0
            ordered = sorted(samples)
            return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]

        p95 = _p95(search_latencies)
        add_p95 = _p95(add_latencies)
        return {
            "status": "ok",
            "version": __version__,
            "active_memories": store.count(),
            "search_p95_ms": round(p95, 1),
            "add_p95_ms": round(add_p95, 1),
            # 红线自证：样本非空时与 config 阈值比对，超限在监控面直接可见
            "search_within_budget": (not search_latencies) or (p95 < settings.search_p95_ms),
            "add_within_budget": (not add_latencies) or (add_p95 < settings.add_budget_ms),
            "llm_model": settings.llm_model,
            "token_usage": llm.usage.total_tokens,
            # LLM 韧性观测（超时/重试/熔断/降级——声明即执法）
            "llm_resilience": llm.resilience.snapshot(),
            # P0-1：部署口径自证（参赛场景/日志模式/适配器/鉴权是否就绪，一目了然）
            "scene": settings.scene,
            "journal_mode": settings.journal_mode,
            "novel_adapter_enabled": settings.novel_adapter_enabled,
            "embedding_provider": settings.embedding_provider,
            "api_key_required": bool(settings.api_key),
        }

    @app.delete("/scope/{scope}", dependencies=[Depends(require_key)])
    def delete_scope(scope: str) -> dict:
        """赛后数据清理（官方：评测数据 30 天内删除；人工兜底，TTL 自动清扫之外）。"""
        return {"scope": scope, "deleted": store.clear_scope(scope)}

    return app


app = create_app()