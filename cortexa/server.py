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
A 层准入（2026-10-30，官方 Q27）：/add /search 走在 429 前先鉴权（匿名请求不占配额），
超软上限在事件循环内排队（不占线程），只有在途 ≥ 硬上限才 429 + Retry-After。
W2 管线：/add = 识别分流(rule_wing) -> 编码标注(encoding) -> 维度地板
(dimension_floor) -> 批量向量化 -> 入库；/search = 双通道召回 + RRF 融合
+ precision-first 截断(modules/search.py)。端点只保留编排与契约转换。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from . import __version__
from .config import Settings, get_settings
from .enhance_stats import stats as enhance_stats
from .journal import TaskJournal
from .llm import LLMClient
from .models import AddRequest, AddResponse, SearchRequest, SearchResponse
from .modules.pipeline import run_add
from .modules.search import search_memory
from .storage import SQLiteMemoryStore

logger = logging.getLogger("cortexa.server")

# ---------------------------------------------------------------------------
# 认证依赖（官方 Authentication：none / Token / Bearer / X-Api-Key 四方案）
# 第二期 api-guide：Memory System Key 可按 `Authorization: Token <key>`、
# `Authorization: Bearer <key>` 或 `X-Api-Key: <key>` 三种头传入，须全部接受。
# ---------------------------------------------------------------------------

_AUTH_SCHEMES = ("bearer ", "token ")


def require_key(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    if not settings.api_key:
        return
    presented = x_api_key
    if presented is None and authorization:
        lowered = authorization.lower()
        for scheme in _AUTH_SCHEMES:
            if lowered.startswith(scheme):
                presented = authorization[len(scheme):].strip()
                break
    if presented != settings.api_key:
        raise HTTPException(status_code=401, detail="invalid or missing API key")


# ---------------------------------------------------------------------------
# 在途准入阀（A-1，2026-10-30）
# ---------------------------------------------------------------------------

class _AdmissionGate:
    """契约面在途请求准入：排队为主、429 只当崩溃阀。

    形状依据（冻结单 /docs 06）：429 不是官方强制实现项，超限若表现为「排队后
    照常返 200」不进任何错误码；主动 429 等于把零风险换成「依赖平台重试次数」。
    所以超软上限先等 admission_wait_ms（等到即照常处理），只有仍在途 ≥ 硬上限
    才拒——避免更糟的 504/连接层失败（那类耗时无控）。
    只在事件循环单线程内计数（await 处才让出），故不需锁。
    """

    def __init__(self, soft_limit: int, hard_limit: int, wait_ms: int) -> None:
        self.soft = max(0, soft_limit)
        self.hard = max(1, hard_limit)
        self.wait_s = max(0, wait_ms) / 1000.0
        self.retry_after_s = max(1, (max(0, wait_ms) + 999) // 1000)
        self.in_flight = 0
        self.peak = 0
        self.waited = 0     # 发生过排队的请求数
        self.rejected = 0   # 返 429 的请求数

    async def acquire(self) -> bool:
        if self.soft and self.in_flight >= self.soft:
            deadline = time.monotonic() + self.wait_s
            self.waited += 1
            while self.in_flight >= self.soft and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
        if self.in_flight >= self.hard:
            self.rejected += 1
            return False
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        return True

    def release(self) -> None:
        self.in_flight -= 1

    def snapshot(self) -> dict:
        return {
            "in_flight": self.in_flight, "peak": self.peak,
            "waited": self.waited, "rejected": self.rejected,
            "soft_limit": self.soft, "hard_limit": self.hard,
            "wait_ms": int(self.wait_s * 1000),
        }


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
        # A 层护栏参数启动即报（不静默）：eval 档 /health 按 Q21 只留最小面，
        # 因此“声明即执法”的自证由本行日志 + app 档 /health admission 计数承担。
        logger.info(
            "A 层护栏：admission soft=%d hard=%d wait=%dms｜search deadline=%dms｜"
            "return_token_budget=%d（<=0=关闭）",
            settings.admission_soft_limit, settings.admission_hard_limit,
            settings.admission_wait_ms, settings.search_deadline_ms,
            settings.search_return_token_budget,
        )
        yield
        # P1-5：服务退出前 drain 增强线程（写库需在 store.close 之前完成）
        try:
            from .enhancer import wait_enhance_threads
            wait_enhance_threads()
        except Exception:
            pass
        store.close()

    app = FastAPI(
        title="Cortexa Memory API",
        version=__version__,
        description="Cortexa — 零模型确定性记忆检索系统（AML Add/Search protocol）",
        lifespan=lifespan,
    )
    # 鉴权依赖必须读本 app 那份 settings（而非全局环境）：否则 create_app(settings)
    # 显式传 api_key 时 /health 报 api_key_required=True 而 /add /search 却不校验。
    app.dependency_overrides[get_settings] = lambda: settings

    # A-1 准入阀：require_key 是 admit 的前置依赖，所以未鉴权请求（401）不会占配额；
    # 排队发生在事件循环内的 async 依赖里，不消耗线程池槽位。
    gate = _AdmissionGate(
        settings.admission_soft_limit,
        settings.admission_hard_limit,
        settings.admission_wait_ms,
    )
    app.state.admission = gate  # 运维/测试可直接读快照，不必走 /health 信息面

    async def admit(
        _authed: Annotated[None, Depends(require_key)],
    ) -> AsyncIterator[None]:
        if not await gate.acquire():
            raise HTTPException(
                status_code=429,
                detail="overloaded, retry later",
                headers={"Retry-After": str(gate.retry_after_s)},
            )
        try:
            yield
        finally:
            gate.release()

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

    @app.post("/add", response_model=AddResponse,
              dependencies=[Depends(require_key), Depends(admit)])
    def add(req: AddRequest) -> AddResponse:
        """同步写入管线（modules/pipeline.run_add，与进程内门面共用同一实现）。"""
        return run_add(store, llm, settings, req, journal=journal)

    # -- Search -------------------------------------------------------------

    @app.post("/search", response_model=SearchResponse,
              dependencies=[Depends(require_key), Depends(admit)])
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
                options=req.options,  # G1：选择题选项参与统一召回（InvMem 查询构造）
            )
        )

    # -- Health / 运维 -------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        # 官方 Q&A（第二期，2026-10-29 文章 Q21）：Health 只用于存活探针，
        # 「不应暴露密钥、模型配置、数据库地址或其他内部信息」——参赛 eval 档
        # 返回最小 2xx 面；真实使用（app 档）保留全量自证面。
        if settings.journal_mode == "eval":
            return {"status": "ok"}

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
            # P1-4：增强/治理/写回失败观察面（不再 except:pass 静默）
            "enhance_stats": enhance_stats.snapshot(),
            # A 层自证：在途/峰值/排队/拒绝计数（"限流已实现"从声明变成可查）
            "admission": gate.snapshot(),
        }

    @app.delete("/scope/{scope}", dependencies=[Depends(require_key)])
    def delete_scope(scope: str) -> dict:
        """赛后数据清理（官方：评测数据 30 天内删除；人工兜底，TTL 自动清扫之外）。"""
        return {"scope": scope, "deleted": store.clear_scope(scope)}

    return app


app = create_app()