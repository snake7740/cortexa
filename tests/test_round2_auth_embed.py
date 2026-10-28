"""第二期（AML 2nd）契约补丁测试：鉴权三方案 + text-embedding-v4 维度参数。

契约依据（2026-10 现行 api-guide）：
- Memory System Key 可按 `Authorization: Token <key>` / `Authorization: Bearer <key>` /
  `X-Api-Key: <key>` 任一方式传入，服务端须全部接受；错 key / 无 key 返回 401
- Health 为无鉴权 GET，任意 2xx 即通过
- 开源方法榜 Embedding 固定 text-embedding-v4（阿里云百炼 compatible-mode，原生 1024 维）；
  存储按 CORTEXA_EMBEDDING_DIM 建矩阵，调用必须显式传 dimensions= 否则向量被静默丢弃
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortexa.config import Settings
from cortexa.llm.client import _supports_dimensions
from cortexa.server import create_app

KEY = "aml-secret-key"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data", embedding_provider="hash", api_key=KEY, _env_file=None
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _add_body() -> dict:
    return {
        "request_id": "r1",
        "user_id": "u1",
        "session_id": "s1",
        "messages": [{"role": "user", "content": "I live in Shanghai."}],
    }


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Token {KEY}"},
        {"Authorization": f"Bearer {KEY}"},
        {"Authorization": f"token {KEY}"},  # 方案名大小写不敏感
        {"X-Api-Key": KEY},
        {"x-api-key": KEY},
    ],
    ids=["token", "bearer", "token-lower", "x-api-key", "x-api-key-lower"],
)
def test_all_official_auth_schemes_accepted(client: TestClient, headers: dict) -> None:
    resp = client.post("/add", json=_add_body(), headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert (body["request_id"], body["user_id"], body["session_id"]) == ("r1", "u1", "s1")

    resp = client.post(
        "/search", json={"query": "where do I live", "user_id": "u1", "top_k": 5}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["data"], list)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": f"Token wrong-{KEY}"},
        {"Authorization": f"Bearer wrong-{KEY}"},
        {"X-Api-Key": "wrong"},
        {"Authorization": KEY},  # 裸 key 无方案名不接受
    ],
    ids=["missing", "token-wrong", "bearer-wrong", "x-api-key-wrong", "bare"],
)
def test_wrong_or_missing_key_rejected(client: TestClient, headers: dict) -> None:
    assert client.post("/add", json=_add_body(), headers=headers).status_code == 401
    assert (
        client.post("/search", json={"query": "q", "user_id": "u1"}, headers=headers).status_code
        == 401
    )


def test_health_needs_no_auth_even_when_key_set(client: TestClient) -> None:
    resp = client.get("/health")
    assert 200 <= resp.status_code < 300
    assert resp.json()["api_key_required"] is True


@pytest.mark.parametrize(
    "model,expected",
    [
        ("text-embedding-v4", True),
        ("text-embedding-v3", True),
        ("text-embedding-3-small", True),
        ("text-embedding-3-large", True),
        ("text-embedding-ada-002", False),
        ("bge-m3", False),
    ],
)
def test_dimensions_param_sent_for_v4_and_3_series(model: str, expected: bool) -> None:
    """AML 开源榜指定 text-embedding-v4；不传 dimensions 会返 1024 维与 512 维存储不符。"""
    assert _supports_dimensions(model) is expected
