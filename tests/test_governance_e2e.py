"""治理 e2e（AML 维度 D，2026-09-04 补）：治理词由 scene schema 决定，server /add 接线生效。

背景漂移（修复前）：examples/local_eval.py 跑 AML 评测却用默认 scene="novel"——
novel schema 无 supersede/update/withdraw 事件词，AML 治理词（currently/no longer
/prefer over 等）全部空转，维度 D 治理在评测路径失效。本测试锁死两条不变量：
1) scene="aml" 时，含 aml update 词（currently/now/…）的新陈述 supersede 旧记忆，
   且旧记忆不再出现在 Search 结果（存储层 ACTIVE 过滤闭环）；
2) scene="novel" 时同一英文句子不触发治理（schema 语义正确隔离——novel 治理词
   是中文死亡/离开事件），防止把治理错误地"一刀切"到所有场景。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortexa.config import Settings
from cortexa.server import create_app


def _client(tmp_path: Path, scene: str) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / ("data-" + scene),
        embedding_provider="hash",
        scene=scene,
        governance_enabled=True,
        _env_file=None,  # 测试不读真实 .env
    )
    return TestClient(create_app(settings))


def _add(client: TestClient, scope: str, request_id: str, content: str, ts: int) -> dict:
    resp = client.post(
        "/add",
        json={
            "request_id": request_id,
            "messages": [{"role": "user", "content": content, "timestamp": ts}],
            "scope": scope,
            "session_id": request_id,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _search(client: TestClient, scope: str, query: str) -> list[str]:
    resp = client.post("/search", json={"query": query, "scope": scope, "top_k": 5})
    assert resp.status_code == 200, resp.text
    return [r["content"] for r in resp.json()["data"]]


# ---------------------------------------------------------------------------
# 不变量 1：aml scene 下维度 D 治理必须生效
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "new_stmt",
    [
        "Alice currently prefers coffee",        # aml update 词 currently
        "Alice now prefers coffee over tea",     # aml update 词 now + prefer over
        "Alice no longer prefers tea",           # aml supersede 词 no longer
    ],
)
def test_aml_scene_supersedes_old_and_excludes_from_search(
    tmp_path: Path, new_stmt: str
) -> None:
    client = _client(tmp_path, "aml")
    old_stmt = "Alice prefers tea over coffee"
    with client:
        _add(client, "sc", "r1", old_stmt, ts=1_000)
        _add(client, "sc", "r2", new_stmt, ts=2_000)
        results = _search(client, "sc", "what drink does Alice prefer now")
        # 维度 D 核心：旧陈述已被取代，不应出现在检索结果；新陈述必须可见
        assert all(old_stmt not in r for r in results), results
        assert any(new_stmt in r for r in results), results


# ---------------------------------------------------------------------------
# 不变量 2：novel scene 不误治英文偏好（schema 语义隔离）
# ---------------------------------------------------------------------------

def test_novel_scene_does_not_supersede_english_preference(tmp_path: Path) -> None:
    """novel 治理词是中文死亡/离开事件——英文偏好更新不属于 novel 语义，不触发。"""
    client = _client(tmp_path, "novel")
    old_stmt = "Alice prefers tea over coffee"
    new_stmt = "Alice currently prefers coffee"
    with client:
        _add(client, "sc", "r1", old_stmt, ts=1_000)
        _add(client, "sc", "r2", new_stmt, ts=2_000)
        results = _search(client, "sc", "what drink does Alice prefer now")
        # novel schema 无 update 词 -> 不 supersede；检索双记忆并存（多事实陈述）
        assert any(old_stmt in r for r in results), results


# ---------------------------------------------------------------------------
# 同 scene 治理对中文规则/事实不误伤（防治理过度）
# ---------------------------------------------------------------------------

def test_aml_scene_keeps_distinct_slots_active(tmp_path: Path) -> None:
    """同 subject 不同 slot 的陈述互不治理：Alice 的饮料偏好不影响她的宠物。"""
    client = _client(tmp_path, "aml")
    with client:
        _add(client, "sc", "r1", "Alice prefers tea over coffee", ts=1_000)
        _add(client, "sc", "r2", "Alice adopted a dog named Rob", ts=2_000)
        _add(client, "sc", "r3", "Alice currently prefers coffee", ts=3_000)
        results = _search(client, "sc", "what is the name of Alice's pet")
        assert any("Rob" in r for r in results), results
