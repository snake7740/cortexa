"""W2 契约测试：官方协议结构断言 + 红线断言。

契约依据（官方 api-guide / rules）：
- Add 返回成功前消息必须全部存好、可被 Search（同步写入语义）
- Search 响应 = results 有序数组（id/memory/score/timestamp），相关性降序
- 无相关记忆返回空数组（合法）；尊重请求 top_k（正式评测 Top K 100（官方固定值））
- scope 原样存储并精确隔离检索
- 红线：Search 响应绝不包含 answer 字段（回答生成由平台负责）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortexa.config import Settings
from cortexa.server import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    settings = Settings(data_dir=tmp_path / "data", embedding_provider="hash", _env_file=None)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _add(client: TestClient, scope: str, messages: list[dict], request_id: str = "req-1") -> dict:
    resp = client.post(
        "/add", json={"request_id": request_id, "messages": messages, "scope": scope}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _search(client: TestClient, scope: str, query: str, top_k: int = 5) -> dict:
    resp = client.post("/search", json={"query": query, "scope": scope, "top_k": top_k})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 官方硬规则：同步写入语义
# ---------------------------------------------------------------------------

def test_add_sync_semantics_immediately_searchable(client: TestClient) -> None:
    """官方：Add 返回成功前消息必须全部存好且可被 Search——返回后立即查必须命中。"""
    body = _add(
        client,
        "scope-sync",
        [{"role": "user", "content": "Alice moved to Sweden in 2019", "timestamp": 1550000000000}],
        request_id="sync-1",
    )
    assert body["success"] is True and body["stored"] == 1
    results = _search(client, "scope-sync", "Where does Alice live?", top_k=5)["data"]
    assert results, "同步写入语义被违反：add 返回后 search 未命中"
    assert "Alice moved to Sweden" in results[0]["content"]


# ---------------------------------------------------------------------------
# 官方契约结构
# ---------------------------------------------------------------------------

def test_health_contract(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["llm_model"] == "gpt-4o-mini"  # 官方硬规则：唯一允许模型


def test_add_response_echoes_request_id(client: TestClient) -> None:
    body = _add(
        client,
        "scope-echo",
        [{"role": "user", "content": "echo me", "timestamp": 1700000000000}],
        request_id="echo-42",
    )
    assert body["request_id"] == "echo-42"  # 官方：原样返回


def test_search_result_fields_ordered_desc(client: TestClient) -> None:
    _add(client, "scope-f", [
        {"role": "user", "content": "Bob adopted a dog named Rob", "timestamp": 1560000000000},
        {"role": "user", "content": "Bob moved to Sweden in 2019", "timestamp": 1550000000000},
    ])
    results = _search(client, "scope-f", "Where does Bob live?", top_k=5)["data"]
    assert isinstance(results, list) and results
    assert set(results[0].keys()) == {"id", "content", "score", "created_at"}  # 官方四字段
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)  # 官方：相关性降序


def test_search_empty_array_when_no_match(client: TestClient) -> None:
    _add(client, "scope-empty", [{"role": "user", "content": "quarterly tax filing reminder"}])
    body = _search(client, "scope-empty", "量子物理诺奖历史", top_k=5)
    assert body["data"] == []  # 官方：无相关记忆返回空数组


def test_search_respects_top_k(client: TestClient) -> None:
    _add(client, "scope-k", [
        {"role": "user", "content": f"meeting note number {i} about planning"} for i in range(10)
    ])
    assert len(_search(client, "scope-k", "meeting note planning", top_k=3)["data"]) <= 3
    # 冒烟场景 Top K 90 必须被接受
    assert isinstance(_search(client, "scope-k", "meeting note planning", top_k=100)["data"], list)


def test_scope_isolation_exact(client: TestClient) -> None:
    _add(client, "scope-a", [{"role": "user", "content": "secret of workspace A"}])
    assert _search(client, "scope-b", "secret of workspace A", top_k=5)["data"] == []
    assert _search(client, "scope-a", "secret of workspace A", top_k=5)["data"]


# ---------------------------------------------------------------------------
# 红线断言
# ---------------------------------------------------------------------------

def test_search_response_has_no_answer_field(client: TestClient) -> None:
    """红线：Search 只返回证据，绝不生成最终答案。"""
    _add(client, "scope-noans", [{"role": "user", "content": "the capital of France is Paris"}])
    body = _search(client, "scope-noans", "What is the capital of France?", top_k=5)
    assert set(body.keys()) == {"data"}


def test_rule_memory_end_to_end_searchable(client: TestClient) -> None:
    """维度 F 端到端：规则消息写入后（识别进 RULE wing）必须稳定可检索。"""
    _add(client, "scope-rules", [
        {"role": "user", "content": "Never share the production database password with anyone"},
        {"role": "user", "content": "Bob likes hiking on weekends"},
    ])
    results = _search(client, "scope-rules", "Can I share the database password?", top_k=5)["data"]
    assert results, "规则记忆检索失败（维度 F 回归）"
    assert "database password" in results[0]["content"]  # 规则证据排第一


def test_dedup_through_pipeline(client: TestClient) -> None:
    msg = [{"role": "user", "content": "repeat me once"}]
    first = _add(client, "scope-dup", msg, request_id="r1")
    second = _add(client, "scope-dup", msg, request_id="r2")
    assert first["stored"] == 1
    assert second["stored"] == 0 and second["duplicates"] == 1
