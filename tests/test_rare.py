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


def _settings(*, adapter: bool = True) -> Settings:
    # RARE-lite 属小说适配器能力：search 触发门控 = rare_enabled and novel_adapter_enabled
    # （P2-5b，2026-09-06 参赛 Search 纯读）——故期望 RARE 生效的用例默认 adapter=True
    return Settings(
        embedding_provider="hash", _env_file=None, rare_enabled=True,
        novel_adapter_enabled=adapter, embedding_dim=32,
    )


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
    # 隔离性：c 与 Alice 无关且未进结果时，不应被 RARE 关联（RARE 只关联命中项）
    rc = store.get(c, scope)
    if rc is not None and c not in got:
        assert c not in ra.links


def test_rare_gated_off_without_adapter_keeps_search_readonly(tmp_path: Path) -> None:
    """P2-5b：novel_adapter_enabled=False（参赛/通用路径）时 Search 不得写记忆桥。

    rare_enabled=True 但适配器关 → RARE-lite 不触发，命中记忆 links_json 保持空——
    评测 Search 路径纯读不变量。
    """
    store = _store(tmp_path)
    scope = "s"
    a = _add(store, scope, "Alice moved to Sweden in 2019")
    b = _add(store, scope, "Alice adopted a dog named Rob")
    results = search_memory(
        store, LLMClient(_settings(adapter=False)), _settings(adapter=False),
        query="Alice", scope=scope, top_k=3,
    )
    assert {r.id for r in results} >= {a, b}
    ra = store.get(a, scope)
    rb = store.get(b, scope)
    assert ra is not None and rb is not None
    assert ra.links == [] and rb.links == []  # 纯读：无 RARE 写回


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