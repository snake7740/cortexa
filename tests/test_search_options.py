"""G1：选择题 options 拼接进统一检索查询（InvMem 查询构造，2026-09-06）。

验证：查询词面与证据无交集、但选项含证据关键实体时，拼接后能召回；
无选项时不硬凑（对照）。"""
from __future__ import annotations

from pathlib import Path

from cortexa.config import Settings
from cortexa.llm import LLMClient
from cortexa.models import AddRequest, AmlMessage
from cortexa.modules.pipeline import run_add
from cortexa.modules.search import search_memory
from cortexa.storage import SQLiteMemoryStore


def _setup(tmp_path: Path) -> tuple[SQLiteMemoryStore, LLMClient, Settings, str]:
    settings = Settings(data_dir=tmp_path, embedding_provider="hash",
                        scene="aml", journal_mode="off", _env_file=None)
    store = SQLiteMemoryStore(str(settings.data_dir / "cortexa.db"),
                              embedding_dim=settings.embedding_dim)
    llm = LLMClient(settings)
    req = AddRequest(request_id="r", scope="s", session_id="s",
                     messages=[AmlMessage(role="user", content=(
                         "Her favorite bakery is called La Boulangerie Bleue."
                     ))])
    run_add(store, llm, settings, req, journal=None)
    return store, llm, settings, "s"


def test_options_concat_recalls_option_entity(tmp_path: Path) -> None:
    store, llm, settings, scope = _setup(tmp_path)
    try:
        opts = ["A. Pain Quotidien", "B. La Boulangerie Bleue", "C. Le Pain"]
        with_opts = search_memory(
            store, llm, settings, query="What is the name of the shop she likes?",
            scope=scope, top_k=3, options=opts,
        )
        assert any("Boulangerie" in r.content for r in with_opts), [
            r.content[:40] for r in with_opts
        ]
        # 对照：无选项时查询与证据无语面交集，不应命中（precision-first）
        no_opts = search_memory(
            store, llm, settings,
            query="What is the name of the shop she likes?",
            scope=scope, top_k=3,
        )
        assert not any("Boulangerie" in r.content for r in no_opts), [
            r.content[:40] for r in no_opts
        ]
    finally:
        store.close()
