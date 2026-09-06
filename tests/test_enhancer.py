"""Enhancer 单测：异步增强（实体补全/关系补边/幂等）。"""

from pathlib import Path

from cortexa.config import Settings
from cortexa.enhancer import Enhancer
from cortexa.llm import LLMClient
from cortexa.models import MemoryInstance, content_hash
from cortexa.schema import get_schema
from cortexa.storage import SQLiteMemoryStore


def _store(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(db_path=str(tmp_path / "e.db"), embedding_dim=8)


def _settings() -> Settings:
    return Settings(embedding_provider="hash", _env_file=None, embedding_dim=8)


def test_enrich_entities_adds_known(tmp_path: Path) -> None:
    store = _store(tmp_path)
    s = _settings()
    m = MemoryInstance(scope="s", content="叶长生拜沈渊为师。", entities=[],
                       content_hash=content_hash("s", "叶长生拜沈渊为师。"))
    store.add(m, embedding=None)
    enh = Enhancer(store, LLMClient(s), s, get_schema("novel"),
                   entities=["叶长生", "沈渊", "苏晚晴"])
    stored = store.get(m.id, "s")
    assert stored is not None
    r = enh.enhance(stored, "s")
    assert r["entities_added"] >= 2  # 叶长生+沈渊
    got = store.get(m.id, "s")
    assert "叶长生" in got.entities and "沈渊" in got.entities
    store.close()


def test_enhance_idempotent_no_dup_entities(tmp_path: Path) -> None:
    store = _store(tmp_path)
    s = _settings()
    m = MemoryInstance(scope="s", content="叶长生走进院子。", entities=["叶长生"],
                       content_hash=content_hash("s", "叶长生走进院子。"))
    store.add(m, embedding=None)
    enh = Enhancer(store, LLMClient(s), s, get_schema("novel"), entities=["叶长生"])
    enh.enhance(store.get(m.id, "s"), "s")
    enh.enhance(store.get(m.id, "s"), "s")  # 重复增强
    got = store.get(m.id, "s")
    assert got.entities.count("叶长生") == 1  # 无重复
    store.close()


def test_infer_relation_adds_link(tmp_path: Path) -> None:
    store = _store(tmp_path)
    s = _settings()
    m = MemoryInstance(scope="s", content="叶长生拜沈渊为师。", entities=[],
                       session_id="ch5", content_hash=content_hash("s", "叶长生拜沈渊为师。"))
    store.add(m, embedding=None)
    enh = Enhancer(store, LLMClient(s), s, get_schema("novel"),
                   entities=["叶长生", "沈渊"])
    r = enh.enhance(store.get(m.id, "s"), "s")
    assert r["relations"] >= 1
    links = store.connection.execute(
        "SELECT src, dst, link_type FROM links WHERE scope = ? AND status = ?", ("s", "ACTIVE"),
    ).fetchall()
    assert any(l["src"] == "叶长生" and l["dst"] == "沈渊" for l in links)
    store.close()


def test_unknown_entity_not_invented(tmp_path: Path) -> None:
    """防幻觉：不在已知名单的实体不加入。"""
    store = _store(tmp_path)
    s = _settings()
    m = MemoryInstance(scope="s", content="叶长生遇到一个路人甲。", entities=[],
                       content_hash=content_hash("s", "叶长生遇到一个路人甲。"))
    store.add(m, embedding=None)
    enh = Enhancer(store, LLMClient(s), s, get_schema("novel"), entities=["叶长生"])
    enh.enhance(store.get(m.id, "s"), "s")
    got = store.get(m.id, "s")
    assert "叶长生" in got.entities
    assert not any("路人甲" in e for e in got.entities)
    store.close()