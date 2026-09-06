"""LLM 韧性层单测（纯逻辑，无 OpenAI 依赖 / 无网络）。

覆盖验收标准：超时快速失败、重试、熔断、降级统计。
"""

import time

from cortexa.llm.resilience import (
    CircuitBreaker,
    CircuitState,
    ResilienceRunner,
    ResilienceStats,
    TimeoutBudget,
    _err_is_retryable,
)


def test_success_path():
    r = ResilienceRunner(timeout_ms=1000, retry_max=1)
    res = r.run(lambda: "hello")
    assert res.ok and res.value == "hello"
    assert not res.retried


def test_non_retryable_fails_fast():
    r = ResilienceRunner(timeout_ms=1000, retry_max=1)
    def boom():
        raise RuntimeError("invalid_request_error 400 bad request")
    res = r.run(boom)
    assert not res.ok
    assert not res.retried  # 400 不重试
    assert r.stats.snapshot()["llm_retries"] == 0


def test_retry_after_429():
    r = ResilienceRunner(timeout_ms=1000, retry_max=1)
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 rate limit exceeded")
        return "ok-after-retry"
    res = r.run(flaky)
    assert res.ok and res.value == "ok-after-retry"
    assert res.retried and calls["n"] == 2
    assert r.stats.snapshot()["llm_retries"] == 1


def test_breaker_opens_and_half_open_recovers():
    b = CircuitBreaker(threshold=3, cooloff_ms=5000)
    assert b.allow() and b.state_name == CircuitState.CLOSED
    for _ in range(3):
        b.record_failure()
    assert b.state_name == CircuitState.OPEN
    assert not b.allow()  # OPEN 拒绝
    # 冷却期后 HALF_OPEN 试探
    b._opened_at = time.monotonic() - 6  # 拨快时钟模拟冷却结束
    assert b.allow() and b.state_name == CircuitState.HALF_OPEN
    b.record_success()
    assert b.state_name == CircuitState.CLOSED


def test_breaker_runner_rejects_when_open():
    r = ResilienceRunner(timeout_ms=1000, retry_max=0, breaker_threshold=2)
    def always_fail():
        raise RuntimeError("502 bad gateway")
    r.run(always_fail)
    r.run(always_fail)  # 第二次后连续失败=2 -> OPEN
    # 第三次：runner 直接拒绝不调用 fn
    called = {"n": 0}
    def spy():
        called["n"] += 1
        return "x"
    res = r.run(spy)
    assert not res.ok and res.breaker_open
    assert called["n"] == 0  # fn 未被调用


def test_timeout_budget_deadline_tightens():
    tb = TimeoutBudget(default_ms=5000, reserve_ratio=0.7)
    assert tb.effective(None) == 5.0
    assert abs(tb.effective(100) - 0.07) < 0.001  # 100ms * 0.7


def test_err_classification():
    assert _err_is_retryable(RuntimeError("429 rate limit"))
    assert _err_is_retryable(RuntimeError("503 service unavailable"))
    assert _err_is_retryable(RuntimeError("connection timeout"))
    assert not _err_is_retryable(RuntimeError("400 invalid api key"))


def test_stats_snapshot():
    s = ResilienceStats()
    s.record_call(); s.record_success(10.0)
    s.record_call(); s.record_failure(timeout=True)
    snap = s.snapshot()
    assert snap["llm_calls"] == 2
    assert snap["llm_successes"] == 1
    assert snap["llm_failures"] == 1
    assert snap["llm_timeouts"] == 1