"""RelationInferrer 单测：显式关系提取（防幻觉约束）。"""

from cortexa.config import Settings
from cortexa.llm import LLMClient
from cortexa.modules.novel_adapter import NovelAdapter
from cortexa.storage import SQLiteMemoryStore
import tempfile, os


def _adapter():
    tmp = tempfile.mkdtemp()
    s = Settings(embedding_provider="hash", _env_file=None)
    store = SQLiteMemoryStore(os.path.join(tmp, "t.db"), embedding_dim=8)
    return NovelAdapter(store, LLMClient(s), s), store


def test_extract_master_apprentice():
    ad, store = _adapter()
    rels = ad.infer_relations("叶长生拜沈渊为师。", ["叶长生", "沈渊"], 1, "s")
    assert len(rels) == 1
    assert rels[0]["src"] == "叶长生" and rels[0]["dst"] == "沈渊"
    assert rels[0]["link_type"] == "师徒"
    store.close()


def test_extract_dao_companion():
    ad, store = _adapter()
    rels = ad.infer_relations("苏晚晴与叶长生结为道侣。", ["苏晚晴", "叶长生"], 1, "s")
    assert len(rels) == 1 and rels[0]["link_type"] == "道侣"
    store.close()


def test_extract_friend():
    ad, store = _adapter()
    rels = ad.infer_relations("周元白是叶长生的好友。", ["周元白", "叶长生"], 1, "s")
    assert len(rels) == 1 and rels[0]["link_type"] == "好友"
    assert rels[0]["src"] == "周元白" and rels[0]["dst"] == "叶长生"
    store.close()


def test_unknown_entity_skipped():
    """防幻觉：捕获到的关系对象不在 known_entities 则跳过。"""
    ad, store = _adapter()
    # 李四未知实体，即使文本有明确关系也不建边
    rels = ad.infer_relations("叶长生拜李四为师。", ["叶长生"], 1, "s")
    assert rels == []
    store.close()


def test_multi_relations_in_text():
    ad, store = _adapter()
    text = "叶长生拜沈渊为师。苏晚晴与叶长生结为道侣。周元白是叶长生的好友。"
    rels = ad.infer_relations(text, ["叶长生", "沈渊", "苏晚晴", "周元白"], 1, "s")
    assert len(rels) == 3
    types = sorted(r["link_type"] for r in rels)
    assert types == ["好友", "师徒", "道侣"]
    store.close()


def test_no_chapter_returns_empty():
    ad, store = _adapter()
    assert ad.infer_relations("叶长生拜沈渊为师。", ["叶长生", "沈渊"], None, "s") == []
    store.close()