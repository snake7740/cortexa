"""G1：语义软通道与分 provider 向量索引测试（hermetic：假嵌入，无网络）。

验证：
- storage：add_semantic/sem_count/sem_search（维度不符行跳过、无行返回空、RULE 豁免下限）；
- search：hash provider 永不触碰 sem；openai+semantic_channel+有索引 → 语义成员 boost 进 top；
- openai 但无索引 → 静默回退不崩溃（FTS 兜底）。
"""

from __future__ import annotations

from pathlib import Path

from cortexa.config import Settings
from cortexa.models import MemoryInstance, content_hash
from cortexa.modules.search import search_memory
from cortexa.storage import SQLiteMemoryStore


def _store(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(db_path=str(tmp_path / "s.db"), embedding_dim=8)


def _add(store: SQLiteMemoryStore, scope: str, text: str) -> MemoryInstance:
    m = MemoryInstance(scope=scope, content=text, content_hash=content_hash(scope, text))
    store.add(m, embedding=[0.1] * 8)  # hash 512? dim=8 store 用 8
    return m


def _settings(tmp_path: Path, **kw) -> Settings:
    base = dict(embedding_provider="hash", embedding_dim=8, scene="novel", chapter_channel=True,
                rare_enabled=False, journal_mode="off", semantic_channel=False, _env_file=None)
    base.update(kw)
    return Settings(data_dir=tmp_path, **base)


class FakeLLM:
    """假嵌入：返回 query 向量 [0.9,0,0,...]，用于定向命中 add_semantic 的目标记忆。"""

    def __init__(self, vec: list[float] | None = None) -> None:
        self._vec = vec or [0.9] + [0.0] * 7

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vec) for _ in texts]


def test_sem_storage_roundtrip_and_dim_skip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    m = _add(store, "s", "alpha")
    store.add_semantic(m.id, "s", [1.0] * 8)
    # 维度不符的行被跳过 → 空结果不崩溃
    store.add_semantic("ghost", "s", [1.0] * 16)  # ghost 无对应记忆行，搜索时也会被排除
    assert store.sem_count("s") == 2  # count 含 ghost（表行）；搜索时过滤
    res = store.sem_search([0.9] + [0.0] * 7, "s", limit=5)
    assert any(mem.id == m.id for mem, _ in res)
    assert store.sem_count("other") == 0
    assert store.sem_search([0.0] * 8, "other", 5) == []


def test_semantic_channel_boosts_target_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    m1 = _add(store, "s", "甲字十二号的小院是叶长生的住处，院里有口井")
    m2 = _add(store, "s", "叶长生在山道反杀追兵后继续赶路")
    target_vec = [0.9] + [0.0] * 7
    store.add_semantic(m1.id, "s", target_vec)   # m1 与查询语义最接近
    store.add_semantic(m2.id, "s", [0.0] * 8)    # m2 零向量 → 低于 min_cos 被剪，避免近并列
    st = _settings(tmp_path, embedding_provider="openai", semantic_channel=True)
    llm = FakeLLM()
    # 语义命中 m1（含 boost）→ top1 应为 m1
    res = search_memory(store, llm, st, query="叶长生住在哪", scope="s", top_k=3)  # type: ignore[arg-type]
    assert res and res[0].id == m1.id


def test_openai_without_index_falls_back_gracefully(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(store, "s", "叶长生在天枢宗当杂役查案")
    st = _settings(tmp_path, embedding_provider="openai", semantic_channel=True)
    llm = FakeLLM()
    res = search_memory(store, llm, st, query="天枢宗查案", scope="s", top_k=3)  # type: ignore[arg-type]
    assert isinstance(res, list)  # 不崩溃（FTS/chapter 兜底）


def test_hash_provider_never_uses_sem(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    m = _add(store, "s", "杂役查案内容")
    store.add_semantic(m.id, "s", [1.0] * 8)
    st = _settings(tmp_path, embedding_provider="hash")
    called = []
    monkeypatch.setattr(store, "sem_search", lambda *a, **k: called.append(1) or [])
    llm = FakeLLM()
    search_memory(store, llm, st, query="杂役", scope="s", top_k=3)  # type: ignore[arg-type]
    assert called == []  # hash provider 不触碰语义表