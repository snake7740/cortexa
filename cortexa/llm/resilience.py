"""LLM 调用韧性层（超时/重试/熔断 纯逻辑，无 OpenAI 依赖）。

设计文档：docs/design/llm-resilience-architecture.md

分层：
- TimeoutBudget:    预算计算（deadline 联动）
- RetryPolicy:      瞬时错误有限重试（预算感知）
- CircuitBreaker:   连续失败熔断（CLOSED/OPEN/HALF_OPEN 状态机）
- ResilienceStats:  观测计数（线程安全）
- ResilienceRunner: 统一编排 超时->熔断->重试

本模块零第三方依赖，可用 pytest 纯单测。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 观测计数
# ---------------------------------------------------------------------------

class ResilienceStats:
    """LLM 韧性层观测计数（线程安全，/health 消费）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.successes = 0
        self.failures = 0
        self.timeouts = 0
        self.retries = 0
        self.fallbacks = 0
        self._latencies_ms: list[float] = []
        self._LAT_MAX = 200

    def record_call(self) -> None:
        with self._lock:
            self.calls += 1

    def record_success(self, latency_ms: float) -> None:
        with self._lock:
            self.successes += 1
            self._latencies_ms.append(latency_ms)
            if len(self._latencies_ms) > self._LAT_MAX:
                self._latencies_ms.pop(0)

    def record_failure(self, *, timeout: bool = False) -> None:
        with self._lock:
            self.failures += 1
            if timeout:
                self.timeouts += 1

    def record_retry(self) -> None:
        with self._lock:
            self.retries += 1

    def record_fallback(self) -> None:
        with self._lock:
            self.fallbacks += 1

    def snapshot(self) -> dict:
        with self._lock:
            lat = sorted(self._latencies_ms)
            p95 = 0.0
            if lat:
                idx = min(len(lat) - 1, int(len(lat) * 0.95))
                p95 = lat[idx]
            return {
                "llm_calls": self.calls,
                "llm_successes": self.successes,
                "llm_failures": self.failures,
                "llm_timeouts": self.timeouts,
                "llm_retries": self.retries,
                "llm_fallbacks": self.fallbacks,
                "llm_p95_ms": round(p95, 1),
            }


# ---------------------------------------------------------------------------
# 重试错误分类
# ---------------------------------------------------------------------------

# 独立状态码检测（避免 "503" 嵌入长串时漏检/误检；429/5xx 视为可重试）
_HTTP_RETRY = re.compile(r"(^|[^0-9])(429|500|502|503|504)([^0-9]|$)")


def _err_is_retryable(err: BaseException) -> bool:
    """判断异常是否可重试（429/5xx/连接/超时）。

    不重试：4xx(除429)/认证/本地参数错误。
    """
    msg = str(err).lower()
    if _HTTP_RETRY.search(msg):
        return True
    if "rate limit" in msg or "quota" in msg:
        return True
    if "timeout" in msg or "timed out" in msg or "deadline" in msg:
        return True
    if "connection" in msg or "connect" in msg:
        return True
    if "api_error" in msg or "server error" in msg:
        return True
    if "exceeded_current_quota" in msg or "insufficient_quota" in msg:
        return True
    return False


# ---------------------------------------------------------------------------
# 超时预算
# ---------------------------------------------------------------------------

class TimeoutBudget:
    """调用预算计算：deadline 与默认超时取更紧者，预留余量。"""

    def __init__(self, default_ms: int, *, reserve_ratio: float = 0.7) -> None:
        self.default_ms = default_ms
        self.reserve_ratio = reserve_ratio  # 只用预算 70%，留 30% 给调用方后续

    def effective(self, deadline_ms: int | None = None) -> float:
        """返回有效超时（秒）。deadline 更紧则收紧，否则用默认。"""
        if deadline_ms is not None and deadline_ms > 0:
            return min(self.default_ms, int(deadline_ms * self.reserve_ratio)) / 1000.0
        return self.default_ms / 1000.0


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------

class CircuitState:
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """连续失败熔断状态机（线程安全）。

    CLOSED -> (连续失败>=threshold) -> OPEN
    OPEN   -> (冷却期过) -> HALF_OPEN
    HALF_OPEN -> (试探成功) -> CLOSED | (试探失败) -> OPEN
    """

    def __init__(self, threshold: int = 3, cooloff_ms: int = 5000) -> None:
        self._lock = threading.RLock()
        self.threshold = max(1, threshold)
        self.cooloff_ms = max(100, cooloff_ms)
        self.state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_inflight = False

    def allow(self) -> bool:
        """是否放行本次调用。配合 record_success/failure 更新状态。"""
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                now = time.monotonic()
                if (now - self._opened_at) * 1000 >= self.cooloff_ms:
                    self.state = CircuitState.HALF_OPEN
                    self._half_open_inflight = True
                    return True
                return False
            # HALF_OPEN
            if self._half_open_inflight:
                return False
            self._half_open_inflight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._half_open_inflight = False
            if self.state != CircuitState.CLOSED:
                self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._half_open_inflight = False
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                self._consecutive_failures = 1
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.threshold:
                self.state = CircuitState.OPEN
                self._opened_at = time.monotonic()

    @property
    def state_name(self) -> str:
        with self._lock:
            return self.state


# ---------------------------------------------------------------------------
# 统一执行器
# ---------------------------------------------------------------------------

@dataclass
class AttemptResult:
    """一次 LLM 调用经韧性层处理后的结果。"""

    ok: bool
    value: object = None
    error: BaseException | None = None
    timed_out: bool = False
    breaker_open: bool = False
    retried: bool = False
    latency_ms: float = 0.0


class ResilienceRunner:
    """把 fn 包进 超时->熔断->重试 编排。

    用法：
        runner = ResilienceRunner(timeout_ms=5000, breaker_threshold=3, retry_max=1)
        result = runner.run(lambda: client.chat.completions.create(...), deadline_ms=None)
        if result.ok: use result.value else: fallback
    """

    def __init__(
        self,
        *,
        timeout_ms: int = 5000,
        retry_max: int = 1,
        retry_base_ms: int = 50,
        breaker_threshold: int = 3,
        breaker_cooloff_ms: int = 5000,
        stats: ResilienceStats | None = None,
    ) -> None:
        self.budget = TimeoutBudget(timeout_ms)
        self.retry_max = max(0, retry_max)
        self.retry_base_ms = max(10, retry_base_ms)
        self.breaker = CircuitBreaker(breaker_threshold, breaker_cooloff_ms)
        self.stats = stats or ResilienceStats()

    def run(
        self, fn, *, deadline_ms: int | None = None, retryable=None
    ) -> AttemptResult:
        """执行一次带韧性的调用。

        fn: 零参数可调用对象（闭包捕获参数）。
        deadline_ms: 调用方剩余预算，收紧超时。
        retryable: 自定义可重试判定 fn(err)->bool；缺省用 _err_is_retryable。
        """
        timeout_s = self.budget.effective(deadline_ms)
        self.stats.record_call()

        # 熔断闸
        if not self.breaker.allow():
            self.stats.record_failure()
            self.stats.record_fallback()
            return AttemptResult(
                ok=False, breaker_open=True, error=RuntimeError("circuit breaker open")
            )

        started = time.monotonic()
        attempts = 0
        retried = False
        is_retryable = retryable or _err_is_retryable

        while attempts <= self.retry_max:
            attempts += 1
            if attempts > 1:
                retried = True
                self.stats.record_retry()
                backoff = self.retry_base_ms
                if deadline_ms and (time.monotonic() - started) * 1000 + backoff > deadline_ms * 0.9:
                    break
                time.sleep(backoff / 1000.0)
            try:
                val = fn()
            except Exception as exc:
                latency = (time.monotonic() - started) * 1000
                timed_out = latency >= timeout_s * 1000 or "timeout" in str(exc).lower()
                self.breaker.record_failure()
                self.stats.record_failure(timeout=timed_out)
                if attempts <= self.retry_max and is_retryable(exc):
                    continue
                self.stats.record_fallback()
                return AttemptResult(
                    ok=False, error=exc, timed_out=timed_out,
                    retried=retried, latency_ms=latency,
                )
            latency = (time.monotonic() - started) * 1000
            self.breaker.record_success()
            self.stats.record_success(latency)
            return AttemptResult(ok=True, value=val, retried=retried, latency_ms=latency)

        # while 正常结束（预算不够重试）
        self.stats.record_fallback()
        return AttemptResult(
            ok=False, error=RuntimeError("budget exhausted before retry"),
            retried=retried, latency_ms=(time.monotonic() - started) * 1000,
        )