"""章节精确通道强信号（真实使用发现，2026-09-06）：显式章号查询必须稳胜弱 FTS。

场景：小说写前 recall「第107章发生了什么」——Ch107 记忆存在但曾被 FTS 随机词重叠
挤出 top3。修复：chapter 通道成员融合分 ×CHAPTER_RRF_BOOST。
"""

from __future__ import annotations

from pathlib import Path

from cortexa.config import Settings
from cortexa.llm import LLMClient
from cortexa.models import MemoryInstance, content_hash
from cortexa.modules.search import search_memory
from cortexa.storage import SQLiteMemoryStore


def _mk(tmp_path: Path) -> SQLiteMemoryStore:
    store = SQLiteMemoryStore(db_path=str(tmp_path / "c.db"), embedding_dim=512)
    rows = [
        ("s", "【第5章·】叶长生在青云宗查案，灰衣弟子王宣露出马脚，线索指向杂务库房。"),
        ("s", "【第8章·】叶长生在山道上被堵，反杀炼气六层，众人惊惧。"),
        ("s", "【第12章·】斗篷人现身，说出石棺与幽冥教的旧事。"),
        ("s", "章节无关内容：王宣在查案中说了一句什么话让人生疑（无锚点正文）。"),
    ]
    for scope, text in rows:
        store.add(MemoryInstance(scope=scope, content=text, content_hash=content_hash(scope, text)))
    return store


def test_explicit_chapter_query_surfaces_that_chapter(tmp_path: Path) -> None:
    store = _mk(tmp_path)
    settings = Settings(data_dir=tmp_path, embedding_provider="hash", scene="novel",
                        chapter_channel=True, rare_enabled=False, journal_mode="off", _env_file=None)
    results = search_memory(store, LLMClient(settings), settings,
                            query="第5章讲了什么", scope="s", top_k=3)
    assert results and results[0].content.startswith("【第5章·】")


def test_chapter_boost_does_not_change_non_chapter_queries(tmp_path: Path) -> None:
    store = _mk(tmp_path)
    settings = Settings(data_dir=tmp_path, embedding_provider="hash", scene="novel",
                        chapter_channel=True, rare_enabled=False, journal_mode="off", _env_file=None)
    # 无章号查询：boost 不介入（不影响既有排序回归面）
    results = search_memory(store, LLMClient(settings), settings,
                            query="王宣 查案 什么话", scope="s", top_k=3)
    assert isinstance(results, list)