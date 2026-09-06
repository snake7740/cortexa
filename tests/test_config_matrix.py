"""T3.2：config 优先级矩阵（显式构造 > 环境变量 > 默认；未知环境忽略）。"""

from __future__ import annotations

from cortexa.config import Settings


def test_explicit_beats_env(monkeypatch) -> None:
    monkeypatch.setenv("CORTEXA_EMBEDDING_PROVIDER", "openai")
    cfg = Settings(embedding_provider="hash", _env_file=None)
    assert cfg.embedding_provider == "hash"  # 显式构造优先


def test_env_over_default(monkeypatch) -> None:
    monkeypatch.setenv("CORTEXA_UNKNOWN_FAKE", "123")  # 未知键忽略
    monkeypatch.setenv("CORTEXA_SEARCH_P95_MS", "250")
    cfg = Settings(_env_file=None)
    assert cfg.search_p95_ms == 250  # env 覆盖默认


def test_t1_defaults_off() -> None:
    cfg = Settings(_env_file=None)
    assert cfg.rrf_lexical_weight == 1.0
    assert cfg.session_rrf_enabled is False
    assert cfg.query_plan_enabled is False
    assert cfg.governance_soft_enabled is False
    assert cfg.time_anchor_enabled is False
    assert cfg.options_dual_channel_enabled is False  # Q2a-D（v3 §5.3）默认关
    assert cfg.options_channel_weight == 0.5
