"""RARE-lite 单测：检索后关联写回（确定性零 LLM）。"""

from pathlib import Path

from cortexa.config import Settings
from cortexa.llm import HashEmbedder, LLMClient
from cortexa.models import MemoryInstance, content_hash
from cortexa.modules.novel_adapter import NovelAdapter
from cortexa.modules.search import search_memory
from cortexa.storage import SQLiteMemoryStore


def _store(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(db_path=str(tmp_path / "r.db"), embedding_dim=32)


def _add(store, scope: str, text: str, mem_type=None) -> str:
    m = MemoryInstance(
        scope=scope, content=text, content_hash=content_hash(scope, text),
        mem_type=mem_type) if mem_type else MemoryInstance(
        scope=scope, content=text, content_hash=content_hash(scope, text),
    )
    store.add(m, embedding=HashEmbedder(32).embed(text))
    return m.id


def _settings() -> Settings:
    return Settings(embedding_provider="hash", _env_file=None, rare_enabled=True, embedding_dim=32)


def test_rare_links_same_topic_results(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = "s"
    a = _add(store, scope, "Alice moved to Sweden in 2019")
    b = _add(store, scope, "Alice adopted a dog named Rob")
    c = _add(store, scope, "Bob likes pizza")
    results = search_memory(
        store, LLMClient(_settings()), _settings(),
        query="Alice", scope=scope, top_k=3,
    )
    # 检索应命中 a/b（同题），可能也命中 c
    got = {r.id for r in results}
    assert a in got  # Alice 相关必命中
    # RARE 已把命中记忆关联写回：至少 a/b 互相关联
    ra = store.get(a, scope)
    rb = store.get(b, scope)
    assert ra is not None and rb is not None
    # a 的 links 应包含 b（若 b 在结果中）
    if b in got:
        assert b in ra.links
        assert a in rb.links


def test_rare_idempotent_no_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = "s"
    a = _add(store, scope, "alpha project plan approved")
    b = _add(store, scope, "alpha project started june")
    adapter = NovelAdapter(store, LLMClient(_settings()), _settings())
    n1 = adapter.rare_strengthen([a, b], scope)
    n2 = adapter.rare_strengthen([a, b], scope)
    assert n1 == 2  # 双向各 +1
    assert n2 == 0  # 幂等
    ra = store.get(a, scope)
    assert ra.links.count(b) == 1


def test_rare_less_than_two_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = "s"
    a = _add(store, scope, "single item")
    adapter = NovelAdapter(store, LLMClient(_settings()), _settings())
    assert adapter.rare_strengthen([a], scope) == 0