"""A 层运行时护栏测试（2026-10-30）：准入阀 429 / Search deadline / 返回预算。

规则依据（docs/competition-rule-freeze-v1.md §其它契约细节 + 官方 /docs 06）：
- Q27 429+Retry-After：429 在平台重试白名单内，但**并非强制实现项**——超限若表现为
  「排队后照常返 200」不进任何错误码。因此形状=「排队为主、429 只当崩溃阀」，
  本组用例锁的正是这个形状：正常负载零拒绝，硬上限触发时必带 Retry-After。
- Q18 返回内容超限被官方截断（低优先级先剪）：我方同向自剪尾部，头部强证据必留。
- Q21 Health 不暴露内部信息：eval 档 /health 仍只有 {"status":"ok"}，准入计数只在
  app 档可见（不因为加了自证就把信息面打开）。
- 鉴权前置：未鉴权请求不得占配额（否则匿名流量能把服务打成 429）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortexa.config import Settings
from cortexa.enhance_stats import stats
from cortexa.llm import LLMClient
from cortexa.models import MemoryInstance, content_hash
from cortexa.modules.search import estimate_tokens, search_memory
from cortexa.server import create_app
from cortexa.storage import SQLiteMemoryStore

KEY = "aml-secret-key"


@pytest.fixture(autouse=True)
def _reset_stats() -> None:
    stats.reset()


def _settings(tmp_path: Path, **kw: object) -> Settings:
    base = dict(data_dir=tmp_path / "data", embedding_provider="hash", api_key=KEY,
                _env_file=None)
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _client(tmp_path: Path, **kw: object) -> TestClient:
    return TestClient(create_app(_settings(tmp_path, **kw)))


def _add_body(rid: str = "r1") -> dict:
    return {
        "request_id": rid,
        "user_id": "u1",
        "session_id": "s1",
        "messages": [{"role": "user", "content": "I live in Shanghai."}],
    }


def _search_body() -> dict:
    return {"query": "where do I live", "user_id": "u1", "top_k": 5}


AUTH = {"Authorization": f"Bearer {KEY}"}


# --------------------------------------------------------------------------
# A-1 准入阀
# --------------------------------------------------------------------------

def test_default_admission_never_fires_under_normal_load(tmp_path: Path) -> None:
    """默认阈值下正常请求零排队零拒绝——参赛档行为与改造前一致。"""
    with _client(tmp_path) as c:
        assert c.post("/add", json=_add_body(), headers=AUTH).status_code == 200
        assert c.post("/search", json=_search_body(), headers=AUTH).status_code == 200
        snap = c.app.state.admission.snapshot()
    assert snap["rejected"] == 0 and snap["waited"] == 0
    assert snap["in_flight"] == 0  # 依赖 teardown 必须释放槽位


def test_over_hard_limit_returns_429_with_retry_after(tmp_path: Path) -> None:
    """在途 ≥ 硬上限：Add/Search 均返 429 且带整数 Retry-After（Q27）。"""
    with _client(tmp_path, admission_soft_limit=1, admission_hard_limit=2,
                 admission_wait_ms=10) as c:
        c.app.state.admission.in_flight = 2
        for path, body in (("/add", _add_body()), ("/search", _search_body())):
            resp = c.post(path, json=body, headers=AUTH)
            assert resp.status_code == 429, resp.text
            ra = resp.headers.get("Retry-After")
            assert ra is not None and int(ra) >= 1
        assert c.app.state.admission.snapshot()["rejected"] == 2

        # 配额回落后同一请求必须照常 200（不是持续封死）
        c.app.state.admission.in_flight = 0
        assert c.post("/search", json=_search_body(), headers=AUTH).status_code == 200


def test_soft_limit_queues_instead_of_rejecting(tmp_path: Path) -> None:
    """超软上限但未到硬上限：排队等待后照常 200，且等待被计数。"""
    with _client(tmp_path, admission_soft_limit=1, admission_hard_limit=512,
                 admission_wait_ms=30) as c:
        c.app.state.admission.in_flight = 1  # 占住软上限
        started = time.perf_counter()
        resp = c.post("/search", json=_search_body(), headers=AUTH)
        waited_ms = (time.perf_counter() - started) * 1000
        assert resp.status_code == 200, resp.text
        snap = c.app.state.admission.snapshot()
    assert snap["waited"] >= 1 and snap["rejected"] == 0
    assert waited_ms >= 25  # 确实等过（而不是立刻放行）


def test_unauthenticated_request_does_not_consume_quota(tmp_path: Path) -> None:
    """鉴权先于准入：满配额下的匿名请求仍返 401，不产生 429（防止穿透面）。"""
    with _client(tmp_path, admission_soft_limit=1, admission_hard_limit=2,
                 admission_wait_ms=10) as c:
        c.app.state.admission.in_flight = 5
        resp = c.post("/search", json=_search_body())
        assert resp.status_code == 401, resp.text
        assert c.app.state.admission.snapshot()["rejected"] == 0


def test_health_exposes_admission_only_in_app_mode(tmp_path: Path) -> None:
    """app 档可自证；eval 档按 Q21 保持最小面，不因自证而打开信息面。"""
    with _client(tmp_path) as c:
        assert "admission" in c.get("/health").json()
    with _client(tmp_path, journal_mode="eval") as c:
        assert c.get("/health").json() == {"status": "ok"}


# --------------------------------------------------------------------------
# A-2 请求内 deadline（降级不报错）
# --------------------------------------------------------------------------

def _store(tmp_path: Path, scope: str = "s", n: int = 6) -> SQLiteMemoryStore:
    store = SQLiteMemoryStore(db_path=str(tmp_path / "t.db"), embedding_dim=8)
    for i in range(n):
        txt = f"Alice memory number {i} about Sweden and dogs"
        store.add(MemoryInstance(scope=scope, content=txt,
                                 content_hash=content_hash(scope, txt)))
    return store


def _recall(store: SQLiteMemoryStore, cfg: Settings, **kw: object) -> list:
    return search_memory(store, LLMClient(cfg), cfg, query="Alice Sweden",
                         scope="s", top_k=5, **kw)  # type: ignore[arg-type]


def test_deadline_hit_skips_expansion_but_still_returns_results(tmp_path: Path) -> None:
    """deadline 到点：跳过邻接扩展（计数可见），返回仍是 200 语义的非空降序结果。"""
    store = _store(tmp_path)
    cfg = _settings(tmp_path, adjacency_enabled=True, search_deadline_ms=0)
    baseline = _recall(store, cfg)
    assert baseline and stats.snapshot()["counts"]["deadline_skip"] == 0

    stats.reset()
    degraded = _recall(store, cfg, deadline_at=time.perf_counter() - 1.0)
    assert degraded, "降级必须返回已算好的证据，不得返空或抛错"
    assert len(degraded) <= 5
    assert [r.score for r in degraded] == sorted([r.score for r in degraded], reverse=True)
    assert stats.snapshot()["counts"]["deadline_skip"] >= 1


def test_deadline_disabled_by_default_setting_is_neutral(tmp_path: Path) -> None:
    """默认阈值（5000ms deadline / 60000 token 预算）下行为与完全关闭时逐一致。"""
    store = _store(tmp_path)
    off = _recall(store, _settings(tmp_path, adjacency_enabled=True,
                                   search_deadline_ms=0,
                                   search_return_token_budget=0))
    on = _recall(store, _settings(tmp_path, adjacency_enabled=True))
    assert [(r.id, r.content, r.score) for r in on] == [
        (r.id, r.content, r.score) for r in off]
    assert stats.snapshot()["counts"]["deadline_skip"] == 0
    assert stats.snapshot()["counts"]["return_trim"] == 0


# --------------------------------------------------------------------------
# A-3 返回预算（官方截断同向自剪）
# --------------------------------------------------------------------------

def test_token_budget_trims_tail_and_keeps_head(tmp_path: Path) -> None:
    """预算收紧：只剪尾部，首条必留，条数不超过 top_k。"""
    store = _store(tmp_path)
    full = _recall(store, _settings(tmp_path, search_return_token_budget=0))
    assert len(full) > 1

    tight = _recall(store, _settings(tmp_path, search_return_token_budget=10))
    assert 1 <= len(tight) < len(full)
    assert [r.id for r in tight] == [r.id for r in full[:len(tight)]]
    assert stats.snapshot()["counts"]["return_trim"] == 1


def test_first_result_survives_absurd_budget(tmp_path: Path) -> None:
    """预算小于一首条也至少保一条（空数组契约合法但必零分）。"""
    store = _store(tmp_path)
    got = _recall(store, _settings(tmp_path, search_return_token_budget=1))
    assert len(got) == 1


def test_estimate_tokens_counts_cjk_heavier_than_ascii() -> None:
    assert estimate_tokens("我在上海居住") == 6            # CJK≈1 token/字
    assert estimate_tokens("abcdefgh") == 2               # ASCII≈4 字符/token
    assert estimate_tokens("") == 0
