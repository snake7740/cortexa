"""官方契约 golden 测试（P0-4 固化）。

数据源：tests/golden/official_aml_contract.json —— 从仓库根 _ref_apiguide.html
（官方 Add/Search API Guide 现行同步规范）逐字抽取。官方 Smoke 回执若有出入，
只改 golden 文件与 cortexa/models.py 契约段别名表，本文件无需改动。

覆盖（对照官方 docs 硬规则）：
1) Add 请求仅声明字段可解析；scope 旧写法兼容（别名）；user_id 新写法是主路径；
2) Add 200 success=true 且 request_id/user_id/session_id 逐字节回显；
3) Search 请求 user_id 隔离；top_k=100（正式固定值）被接受且 data 长度受控；
4) Search 200 data 数组 + 元素必含非空 id/content；响应无 answer 字段；
5) 格式错误（缺 request_id）→ 422；Health 无鉴权 2xx；
6) 30 天 TTL：eval scope 过期自动清扫、app scope 不删（storage 层）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortexa.config import Settings
from cortexa.server import create_app
from cortexa.storage import SQLiteMemoryStore

GOLDEN = Path(__file__).parent / "golden" / "official_aml_contract.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data", embedding_provider="hash", _env_file=None
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_golden_required_keys(golden: dict) -> None:
    """golden 样例结构 = 官方契约的机器可读快照。"""
    assert set(golden["add_request"]) == {"request_id", "messages", "user_id", "session_id"}
    assert set(golden["search_request"]) == {"query", "options", "user_id", "top_k"}
    assert list(golden["add_response_200"]) == ["success", "request_id", "user_id", "session_id"]
    assert set(golden["search_response_200"]) == {"data"}
    item = golden["search_response_200"]["data"][0]
    assert {"id", "content"} <= set(item)


def test_add_official_payload_echoes_ids(client: TestClient, golden: dict) -> None:
    """官方 Add 请求（只有 user_id，无 scope 旧键）必须可解析且三 ID 逐字节回显。"""
    req = golden["add_request"]
    resp = client.post("/add", json=req)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["request_id"] == req["request_id"]
    assert body["user_id"] == req["user_id"]
    assert body["session_id"] == req["session_id"]
    # 同步语义：返回后立即可检索
    found = client.post(
        "/search",
        json={"query": "memory text", "user_id": req["user_id"], "top_k": 5},
    ).json()["data"]
    assert any("memory text" in h["content"] for h in found)


def test_search_official_payload_shape_and_isolation(
    client: TestClient, golden: dict
) -> None:
    """官方 Search 请求形态 + user_id 精确隔离 + 响应 data 元素契约。"""
    req = golden["search_request"]
    other = "user:" + req["user_id"]
    # 只往 req["user_id"] 存
    client.post(
        "/add",
        json={
            "request_id": "iso-1", "messages": [{"role": "user", "content": "Alice moved to Sweden"}],
            "user_id": req["user_id"], "session_id": "s-iso",
        },
    )
    hit = client.post("/search", json={"query": "Alice moved", "user_id": req["user_id"], "top_k": 100})
    assert hit.status_code == 200
    data = hit.json()["data"]
    assert isinstance(data, list) and data
    assert set(data[0]) >= {"id", "content"}  # id/content 必填非空
    assert data[0]["id"] and data[0]["content"]
    assert "answer" not in hit.json()  # 红线：绝不返回最终答案
    # 跨 user_id 检索 = 空数组（官方：跨 user_id 返回记忆属违规）
    assert client.post("/search", json={"query": "Alice moved", "user_id": other, "top_k": 5}).json()["data"] == []


def test_top_k_100_formal_accepted(client: TestClient) -> None:
    """正式外部评测 Top K=100 必须被接受；返回不得超过 top_k。"""
    client.post(
        "/add",
        json={"request_id": "tk", "messages": [{"role": "user", "content": f"note {i}"} for i in range(60)],
              "user_id": "u-tk", "session_id": "s-tk"},
    )
    data = client.post("/search", json={"query": "note", "user_id": "u-tk", "top_k": 100}).json()["data"]
    assert isinstance(data, list)
    assert len(data) <= 100


def test_legacy_scope_alias_still_works(client: TestClient) -> None:
    """旧客户端 scope 键兼容（内部字段名即 scope，populate_by_name）。"""
    r1 = client.post("/add", json={"request_id": "leg", "messages": [{"role": "user", "content": "legacy text"}],
                                   "scope": "legacy-scope", "session_id": "s"})
    assert r1.status_code == 200
    out = client.post("/search", json={"query": "legacy", "scope": "legacy-scope", "top_k": 5}).json()
    assert out["data"] and "legacy text" in out["data"][0]["content"]


def test_health_any_2xx_no_auth(client: TestClient) -> None:
    """官方：Health 无鉴权 GET，任意 2xx 即正常。"""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    # P0-1 自证：部署口径可见
    assert "scene" in body and "journal_mode" in body and "api_key_required" in body


def test_missing_request_id_is_422(client: TestClient) -> None:
    """格式错误：缺必填字段 → 422（官方：契约错不自动重试）。"""
    resp = client.post("/add", json={"messages": [{"role": "user", "content": "x"}], "user_id": "u"})
    assert resp.status_code == 422


def test_ttl_sweep_only_eval_expired(tmp_path: Path) -> None:
    """P0-3：eval 来源且超 30 天 → 自动清扫；app 来源永不删。"""
    store = SQLiteMemoryStore(db_path=str(tmp_path / "ttl.db"), embedding_dim=8)
    now = int(time.time() * 1000)
    old = now - 31 * 86_400_000
    store.record_scope("eval-old", source="eval")
    store.record_scope("app-old", source="app")
    # 直接把 eval-old 的最后写入时间改到 31 天前（绕过惰性登记）
    store.connection.execute(
        "UPDATE scope_meta SET last_write_ms=? WHERE scope='eval-old'", (old,)
    )
    store.connection.commit()
    cleared = store.sweep_expired_eval_scopes(now_ms=now)
    assert "eval-old" in cleared
    assert "app-old" not in cleared
    assert store.count("eval-old") == 0
    assert store.count("app-old") == 0  # app 未写入过记忆，仅元数据，不误删
    # app scope 元数据仍在，eval 元数据已清
    rows = {r["scope"] for r in store.scope_meta_rows()}
    assert "app-old" in rows and "eval-old" not in rows
