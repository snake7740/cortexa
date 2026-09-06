"""Facade 类库门面测试：三真实场景（小说/代码/学术）+ 双入口一致性（ADR-0005）。

双入口单管线是"真实使用反馈反哺参赛管线"不失真的保证：类库写入必须能被
HTTP 服务检索，反之亦然。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortexa import Cortexa
from cortexa.config import Settings
from cortexa.server import create_app
from cortexa.storage import _fts_escape


@pytest.fixture()
def cm(tmp_path: Path) -> Cortexa:
    with Cortexa(data_dir=tmp_path / "lib") as facade:
        yield facade


class TestRealWorldScenarios:
    """真实使用三场景基线（dogfooding 反馈的回归锚点）。"""

    def test_novel_fact_recall_cjk(self, cm: Cortexa) -> None:
        cm.remember("林晚在第三章把怀表交给了沈屹", scope="novel", timestamp=1700000000000)
        hits = cm.recall_texts("怀表交给了谁", scope="novel")
        assert hits and "怀表" in hits[0]

    def test_code_rule_recall(self, cm: Cortexa) -> None:
        cm.remember("这个项目的包管理器统一用 pnpm，不要用 npm", scope="code")
        hits = cm.recall_texts("包管理器用什么", scope="code")
        assert hits and "pnpm" in hits[0]

    def test_academic_paper_recall(self, cm: Cortexa) -> None:
        cm.remember("Transformer 论文于 2017 年提出 self-attention 机制", scope="papers")
        hits = cm.recall_texts("self-attention 出自哪篇论文", scope="papers")
        assert hits and "self-attention" in hits[0]

    def test_scope_isolation_between_projects(self, cm: Cortexa) -> None:
        cm.remember("novel-only secret", scope="novel")
        assert cm.recall_texts("novel-only secret", scope="code") == []


class TestDualEntrySamePipeline:
    """双入口单管线：类库与 HTTP 服务读写互通。"""

    def test_facade_write_http_read(self, tmp_path: Path) -> None:
        data = tmp_path / "shared"
        with Cortexa(data_dir=data) as cm:
            cm.remember("Never deploy to production on Friday afternoon", scope="s")
            settings = Settings(data_dir=data, embedding_provider="hash", _env_file=None)
            with TestClient(create_app(settings)) as client:
                body = client.post(
                    "/search", json={"query": "when can we deploy?", "scope": "s", "top_k": 5}
                ).json()
                assert body["data"] and "deploy" in body["data"][0]["content"]

    def test_http_write_facade_read(self, tmp_path: Path) -> None:
        data = tmp_path / "shared2"
        settings = Settings(data_dir=data, embedding_provider="hash", _env_file=None)
        with TestClient(create_app(settings)) as client:
            resp = client.post(
                "/add",
                json={
                    "request_id": "r1",
                    "scope": "s",
                    "messages": [{"role": "user", "content": "Alice moved to Sweden in 2019"}],
                },
            )
            assert resp.status_code == 200, resp.text
        with Cortexa(data_dir=data) as cm:
            hits = cm.recall_texts("Where does Alice live?", scope="s")
            assert hits and "Sweden" in hits[0]


class TestManagement:
    def test_stats_and_forget_scope(self, cm: Cortexa) -> None:
        cm.remember("a fact", scope="s1")
        cm.remember("another fact", scope="s2")
        assert cm.stats()["active_memories"] == 2
        assert cm.stats(scope="s1")["active_memories"] == 1
        assert cm.forget_scope("s1") == 1
        assert cm.stats()["active_memories"] == 1

    def test_openai_key_auto_switches_provider(self, tmp_path: Path) -> None:
        cm = Cortexa(data_dir=tmp_path / "x", openai_api_key="sk-test")
        try:
            assert cm.stats()["embedding_provider"] == "openai"
        finally:
            cm.close()

    def test_llm_model_fixed_official_rule(self, cm: Cortexa) -> None:
        assert cm.stats()["llm_model"] == "gpt-4o-mini"  # 官方硬规则


class TestCjkBigramFts:
    """中文对称切分：索引侧 _fts_index_text 与查询侧 _fts_escape 同规则。"""

    def test_cjk_query_expands_to_bigrams(self) -> None:
        expr = _fts_escape("怀表交给了谁")
        assert '"怀表"' in expr and '"交给"' in expr

    def test_mixed_cjk_ascii(self) -> None:
        expr = _fts_escape("用 pnpm 安装依赖")
        assert '"pnpm"' in expr and '"安装"' in expr and '"依赖"' in expr

    def test_ascii_stopwords_filtered(self) -> None:
        """查询虚词（where/does）被停用词过滤，实词保留（2026-09-04 维度 G 修复）。"""
        expr = _fts_escape("where does Alice live")
        assert '"where"' not in expr and '"does"' not in expr
        assert '"Alice"' in expr and '"live"' in expr

    def test_ascii_unchanged(self) -> None:
        """旧断言保留（兼容命名）：实词仍原样保留。"""
        expr = _fts_escape("Alice prefers tea over coffee")
        assert '"Alice"' in expr and '"coffee"' in expr


class TestEndpointConfig:
    """端点配置解析（不绑定供应商：任意 OpenAI 兼容云端）。

    优先级：显式构造参数 > config_file/.env 自动探测 > 无前后缀环境变量 > 默认。
    """

    def test_explicit_config_file_loaded(self, tmp_path: Path) -> None:
        cfg = tmp_path / "my-endpoint.env"
        cfg.write_text(
            "CORTEXA_OPENAI_BASE_URL=https://relay.example.com/v1\n"
            "CORTEXA_OPENAI_API_KEY=sk-file-key\n",
            encoding="utf-8",
        )
        with Cortexa(data_dir=tmp_path / "lib", config_file=cfg) as cm:
            assert cm._settings.openai_base_url == "https://relay.example.com/v1"
            assert cm._settings.openai_api_key == "sk-file-key"
            # 文件给到 key 且未显式指定 provider → 自动切 openai 嵌入
            assert cm._settings.embedding_provider == "openai"

    def test_data_dir_env_auto_discovery(self, tmp_path: Path) -> None:
        data = tmp_path / "lib"
        data.mkdir()
        (data / ".env").write_text(
            "CORTEXA_OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n",
            encoding="utf-8",
        )
        with Cortexa(data_dir=data) as cm:
            assert cm._settings.openai_base_url.endswith("compatible-mode/v1")

    def test_unprefixed_env_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 生态约定俗成的无前后缀变量兑底（用户已在 shell/系统层配好 key 的场景）
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
        with Cortexa(data_dir=tmp_path / "lib") as cm:
            assert cm._settings.openai_api_key == "sk-env-key"
            assert cm._settings.openai_base_url == "https://api.deepseek.com/v1"
            assert cm._settings.embedding_provider == "openai"

    def test_ctor_kwarg_beats_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_BASE_URL", "https://from-env.example/v1")
        with Cortexa(
            data_dir=tmp_path / "lib", openai_base_url="https://from-kwarg.example/v1"
        ) as cm:
            assert cm._settings.openai_base_url == "https://from-kwarg.example/v1"

    def test_env_file_beats_unprefixed_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 配置文件是用户的持久化选择，优先于机器级环境变量兑底
        data = tmp_path / "lib"
        data.mkdir()
        (data / ".env").write_text(
            "CORTEXA_OPENAI_BASE_URL=https://from-file.example/v1\n", encoding="utf-8"
        )
        monkeypatch.setenv("OPENAI_BASE_URL", "https://from-env.example/v1")
        with Cortexa(data_dir=data) as cm:
            assert cm._settings.openai_base_url == "https://from-file.example/v1"

    def test_no_config_stays_offline_hash(self, tmp_path: Path) -> None:
        # 无任何端点配置 → 离线 hash（零成本、可复现的默认态）
        with Cortexa(data_dir=tmp_path / "lib") as cm:
            assert cm._settings.embedding_provider == "hash"
            assert cm._settings.openai_api_key == ""

    def test_unprefixed_vars_inside_env_file(self, tmp_path: Path) -> None:
        # 用户习惯写法：配置文件内直接写 OPENAI_*（无 CORTEXA_ 前缀）同样生效——
        # pydantic 只认前缀变量，无前后缀的由兑底层解析（Kimi/任意云端场景实证）
        data = tmp_path / "lib"
        data.mkdir()
        (data / ".env").write_text(
            "OPENAI_API_KEY=sk-file-plain\n"
            "OPENAI_BASE_URL=https://api.moonshot.cn/v1\n",
            encoding="utf-8",
        )
        with Cortexa(data_dir=data) as cm:
            assert cm._settings.openai_api_key == "sk-file-plain"
            assert cm._settings.openai_base_url == "https://api.moonshot.cn/v1"
            assert cm._settings.embedding_provider == "openai"

    def test_prefixed_beats_unprefixed_in_file(self, tmp_path: Path) -> None:
        # 同一文件两种写法并存：CORTEXA_ 前缀优先（显式选择 > 习惯写法）
        data = tmp_path / "lib"
        data.mkdir()
        (data / ".env").write_text(
            "CORTEXA_OPENAI_BASE_URL=https://prefixed.example/v1\n"
            "OPENAI_BASE_URL=https://plain.example/v1\n",
            encoding="utf-8",
        )
        with Cortexa(data_dir=data) as cm:
            assert cm._settings.openai_base_url == "https://prefixed.example/v1"
