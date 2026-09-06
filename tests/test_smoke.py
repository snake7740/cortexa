"""W1 冒烟测试：契约结构 + scope 隔离 + 同步写入语义 + 红线不变量。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortexa.config import Settings
from cortexa.models import MemoryInstance, MemoryType, content_hash
from cortexa.server import create_app
from cortexa.storage import RuleImmortalError, SQLiteMemoryStore


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        embedding_provider="hash",
        _env_file=None,  # 测试不读真实 .env
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _add(client: TestClient, scope: str, messages: list[dict], request_id: str = "req-1") -> dict:
    resp = client.post(
        "/add",
        json={"request_id": request_id, "messages": messages, "scope": scope, "session_id": "s1"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _search(client: TestClient, scope: str, query: str, top_k: int = 5) -> dict:
    resp = client.post(
        "/search", json={"query": query, "scope": scope, "top_k": top_k}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 契约结构
# ---------------------------------------------------------------------------

def test_health_contract(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["llm_model"] == "gpt-4o-mini"  # 官方硬规则：唯一允许模型


def test_add_response_echoes_request_id(client: TestClient) -> None:
    body = _add(client, "scope-a", [{"role": "user", "content": "我喜欢番茄炒蛋", "timestamp": 1700000000000}])
    assert body["request_id"] == "req-1"  # 官方：echo request_id
    assert body["success"] is True
    assert body["stored"] == 1


def test_search_result_fields_ordered_desc(client: TestClient) -> None:
    _add(client, "scope-a", [
        {"role": "user", "content": "Alice moved to Sweden in 2019", "timestamp": 1550000000000},
        {"role": "user", "content": "Alice adopted a dog named Rob", "timestamp": 1560000000000},
    ])
    body = _search(client, "scope-a", "Where does Alice live?", top_k=5)
    results = body["data"]
    assert isinstance(results, list)
    if results:
        assert set(results[0].keys()) == {"id", "content", "score", "created_at"}  # 官方四字段
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)  # 官方：相关性降序


def test_search_returns_empty_array_when_no_match(client: TestClient) -> None:
    _add(client, "scope-x", [{"role": "user", "content": "completely unrelated text"}])
    body = _search(client, "scope-x", "量子物理诺奖历史", top_k=5)
    assert body["data"] == []  # 官方：无相关记忆返回空数组


def test_search_respects_top_k(client: TestClient) -> None:
    _add(client, "scope-k", [
        {"role": "user", "content": f"meeting note number {i} about planning"} for i in range(10)
    ])
    body = _search(client, "scope-k", "meeting note planning", top_k=3)
    assert len(body["data"]) <= 3  # 官方：允许返回的最大条数


def test_add_accepts_top_k_90_like_smoke(client: TestClient) -> None:
    """冒烟场景：Top K 90 必须被接受（官方正式评测 Top K 100（官方固定值））。"""
    _add(client, "scope-smoke", [{"role": "user", "content": "user prefers window seats"}])
    body = _search(client, "scope-smoke", "seat preference", top_k=100)
    assert isinstance(body["data"], list)


# ---------------------------------------------------------------------------
# scope 隔离与去重（官方：检索隔离以 scope 为准）
# ---------------------------------------------------------------------------

def test_scope_isolation(client: TestClient) -> None:
    _add(client, "scope-a", [{"role": "user", "content": "secret of workspace A"}])
    body = _search(client, "scope-b", "secret of workspace A", top_k=5)
    assert body["data"] == []


def test_dedup_within_scope(client: TestClient) -> None:
    msg = [{"role": "user", "content": "repeat me once"}]
    first = _add(client, "scope-dup", msg, request_id="r1")
    second = _add(client, "scope-dup", msg, request_id="r2")
    assert first["stored"] == 1
    assert second["stored"] == 0 and second["duplicates"] == 1


# ---------------------------------------------------------------------------
# 红线不变量：RULE 记忆永不衰减/遗忘（维度 F）
# ---------------------------------------------------------------------------

def test_rule_memory_cannot_be_forgotten(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(db_path=str(tmp_path / "t.db"), embedding_dim=8)
    mem = MemoryInstance(
        scope="s", content="不要用 Java 写新服务", mem_type=MemoryType.RULE,
        no_decay=True, content_hash=content_hash("s", "不要用 Java 写新服务"),
    )
    assert store.add(mem) is True
    with pytest.raises(RuleImmortalError):
        store.forget(mem.id, "s")  # 100% 拒触
    assert store.get(mem.id, "s").status.value == "ACTIVE"


def test_supersede_marks_old_state_with_valid_to(tmp_path: Path) -> None:
    """矛盾覆盖：旧状态标记过期但保留可追溯（维度 C 双时间轴）。"""
    store = SQLiteMemoryStore(db_path=str(tmp_path / "t2.db"), embedding_dim=8)
    old = MemoryInstance(scope="s", content="user lives in Paris", content_hash=content_hash("s", "user lives in Paris"))
    new = MemoryInstance(scope="s", content="user lives in Berlin", content_hash=content_hash("s", "user lives in Berlin"))
    store.add(old)
    store.add(new)
    store.supersede(old.id, new.id, "s")
    assert store.get(old.id, "s").status.value == "SUPERSEDED"
    assert store.get(old.id, "s").superseded_by == new.id


def test_fsrs_retrievability_formula() -> None:
    """R(t) = (1 + t/(9*S))^(-1)（业务文档 8.1）。"""
    import time as _t

    from cortexa.models import FSRSState

    state = FSRSState(stability=1.0, last_review_at=_t.time())
    now = state.last_review_at + 9 * 86400  # 恰好 9*S 天
    assert abs(state.retrievability(now=now) - 0.5) < 1e-6


def test_hash_embedding_deterministic() -> None:
    from cortexa.llm import HashEmbedder

    e = HashEmbedder(dim=64)
    assert e.embed("hello world") == e.embed("hello world")
    assert e.embed("你好世界") != e.embed("hello world")
