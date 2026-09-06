"""W2 单元测试：rule_wing 识别分流 / encoding 编码 / dimension_floor 审计
/ search 融合管线 / 存储不变量（RULE 永不衰减）。

红线不变量（业务文档 12.3）：RULE 记忆永不衰减/遗忘（store.forget 100% 拒触）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortexa.config import Settings
from cortexa.llm import HashEmbedder, LLMClient
from cortexa.models import AmlMessage, MemoryInstance, MemoryType, content_hash
from cortexa.modules.dimension_floor import (
    DimensionFloorError,
    assert_floor,
    audit,
    enforce,
)
from cortexa.modules.encoding import encode_message, extract_entities
from cortexa.modules.rule_wing import classify, classify_message, route_wing
from cortexa.modules.search import search_memory
from cortexa.storage import RuleImmortalError, SQLiteMemoryStore

# ---------------------------------------------------------------------------
# rule_wing：识别分流
# ---------------------------------------------------------------------------

class TestRuleWing:
    @pytest.mark.parametrize("text", [
        "You must enable 2FA on the admin account",
        "Never deploy on Friday afternoon",
        "不要在生产环境打印密钥",
        "必须使用 Python 3.12",
        "Always use parameterized queries",
    ])
    def test_rule(self, text: str) -> None:
        assert classify(text).mem_type == MemoryType.RULE

    @pytest.mark.parametrize("text", [
        "I really like spicy food",
        "My favorite editor is Vim",
        "她对花生过敏",
        "I prefer window seats",
    ])
    def test_preference(self, text: str) -> None:
        assert classify(text).mem_type == MemoryType.PREFERENCE

    @pytest.mark.parametrize("text", [
        "Alice moved to Sweden in 2019",
        "他们去年结婚了",
    ])
    def test_event(self, text: str) -> None:
        assert classify(text).mem_type == MemoryType.EVENT

    @pytest.mark.parametrize("text", [
        "How to reset the router password",
        "第一步：安装依赖",
    ])
    def test_procedure(self, text: str) -> None:
        assert classify(text).mem_type == MemoryType.PROCEDURE

    @pytest.mark.parametrize("text", [
        "The build fails with an error",
        "抛出 NullPointerException 异常",
    ])
    def test_code_debug(self, text: str) -> None:
        assert classify(text).mem_type == MemoryType.CODE_DEBUG

    @pytest.mark.parametrize("text", [
        "commit and push to main",
        "部署到 staging 环境",
    ])
    def test_code_dev(self, text: str) -> None:
        assert classify(text).mem_type == MemoryType.CODE_DEV

    def test_fact_default(self) -> None:
        cls = classify("The meeting is at 3pm")
        assert cls.mem_type == MemoryType.FACT and cls.source == "default"

    def test_llm_fallback_without_llm_degrades(self) -> None:
        """无 LLM（llm=None）时默认 FACT 兜底，绝不抛异常阻断写入管线。"""
        cls = classify_message("some neutral statement", None)
        assert cls.mem_type == MemoryType.FACT and cls.source == "default"

    def test_rule_hit_skips_llm_even_if_available(self) -> None:
        class _NeverLLM:
            def available(self) -> bool:
                return True

            def can_sync_classify(self) -> bool:
                return True  # 名义可用；规则命中仍不应走到 LLM

            def chat(self, *a: object, **k: object) -> str:  # pragma: no cover
                raise AssertionError("规则命中后不应调用 LLM")

        cls = classify_message("You must wear a badge", _NeverLLM())  # type: ignore[arg-type]
        assert cls.mem_type == MemoryType.RULE and cls.source == "rule"

    def test_route_wing(self) -> None:
        rule_cls = classify("必须走 code review")
        fact_cls = classify("The sky is blue")
        assert route_wing(rule_cls) == "rules"
        assert route_wing(fact_cls) == "general"


# ---------------------------------------------------------------------------
# encoding：编码层
# ---------------------------------------------------------------------------

def _encode(text: str, mem_type: MemoryType) -> MemoryInstance:
    from cortexa.modules.rule_wing import Classification

    cls = Classification(mem_type, 0.85, "rule", "x")
    return encode_message(
        AmlMessage(role="user", content=text, timestamp=1550000000000),
        scope="s",
        session_id="sess",
        request_id="r1",
        cls=cls,
    )


class TestEncoding:
    def test_rule_memory_gets_no_decay_and_wing(self) -> None:
        from cortexa.modules.rule_wing import classify

        mem = encode_message(
            AmlMessage(content="Never share credentials"),
            scope="s", session_id=None, request_id="r", cls=classify("Never share credentials"),
        )
        assert mem.mem_type == MemoryType.RULE
        assert mem.no_decay is True
        assert mem.wing == "rules"
        assert mem.room == "constraints"
        assert mem.intent == "constraint"

    def test_content_hash_scoped(self) -> None:
        a = _encode("same text", MemoryType.FACT)
        assert a.content_hash == content_hash("s", "same text")

    def test_entities_extraction(self) -> None:
        entities = extract_entities("Alice moved to Sweden and adopted \"Rob\" (alice@example.com)")
        assert "Alice" in entities and "Sweden" in entities
        assert "Rob" in entities and "alice@example.com" in entities

    def test_entities_stopwords_filtered(self) -> None:
        assert "The" not in extract_entities("The build fails")
        assert extract_entities("nothing here at all") == []

    def test_event_timestamp_propagates(self) -> None:
        mem = _encode("x", MemoryType.FACT)
        assert mem.event_ts_ms == 1550000000000 and mem.valid_from_ms == 1550000000000


# ---------------------------------------------------------------------------
# dimension_floor：维度审计
# ---------------------------------------------------------------------------

class TestDimensionFloor:
    def test_rule_memory_passes_floor(self) -> None:
        mem = _encode("不要用 root 直连数据库", MemoryType.RULE)
        assert audit(mem).passed  # RULE + PROCEDURAL + CONFIDENCE 全齐

    def test_no_decay_without_rule_type_fails(self) -> None:
        mem = MemoryInstance(scope="s", content="x", content_hash="h", no_decay=True)
        report = audit(mem)
        assert not report.passed and {"RULE", "PROCEDURAL"} <= report.missing

    def test_generic_floor_requires_content(self) -> None:
        mem = MemoryInstance(scope="s", content="   ", content_hash="h")
        assert "CONTENT" in audit(mem).missing

    def test_enforce_fixes_confidence(self) -> None:
        mem = MemoryInstance(scope="s", content="x", content_hash="h", confidence=0.0)
        enforce(mem)
        assert mem.confidence == 0.5

    def test_assert_floor_raises(self) -> None:
        mem = MemoryInstance(scope="s", content="", content_hash="h")
        with pytest.raises(DimensionFloorError):
            assert_floor(mem)


# ---------------------------------------------------------------------------
# search：融合管线
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(db_path=str(tmp_path / "s.db"), embedding_dim=512)


def _settings() -> Settings:
    return Settings(embedding_provider="hash", _env_file=None)


class TestSearchModule:
    def test_fusion_returns_desc_and_hits(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        llm = LLMClient(_settings())
        for text in ["Alice moved to Sweden in 2019", "Alice adopted a dog named Rob"]:
            store.add(MemoryInstance(
                scope="s", content=text, content_hash=content_hash("s", text),
            ), embedding=HashEmbedder(512).embed(text))
        results = search_memory(
            store, llm, _settings(), query="Where does Alice live?", scope="s", top_k=5
        )
        assert len(results) == 2
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_rule_boost_orders_rule_first(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        embedder = HashEmbedder(512)
        rule = MemoryInstance(
            scope="s", content="Always use Python 3.12 for new services",
            content_hash=content_hash("s", "Always use Python 3.12 for new services"),
            mem_type=MemoryType.RULE,
        )
        fact = MemoryInstance(
            scope="s", content="Python 3.12 for new services is common nowadays",
            content_hash=content_hash("s", "Python 3.12 for new services is common nowadays"),
        )
        for m in (rule, fact):
            store.add(m, embedding=embedder.embed(m.content))
        results = search_memory(
            store, LLMClient(_settings()), _settings(),
            query="python 3.12 for new services", scope="s", top_k=5,
        )
        assert len(results) == 2
        assert results[0].memory == rule.content  # RULE 加成后稳定排第一

    def test_empty_store_returns_empty(self, tmp_path: Path) -> None:
        results = search_memory(
            _make_store(tmp_path), LLMClient(_settings()), _settings(),
            query="anything", scope="none", top_k=5,
        )
        assert results == []

    def test_top_k_clamped(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        embedder = HashEmbedder(512)
        for i in range(5):
            text = f"note item {i} planning"
            store.add(MemoryInstance(scope="s", content=text, content_hash=content_hash("s", text)),
                      embedding=embedder.embed(text))
        results = search_memory(
            store, LLMClient(_settings()), _settings(),
            query="note item planning", scope="s", top_k=0,  # 0 -> 夹到 1
        )
        assert len(results) == 1


# ---------------------------------------------------------------------------
# 存储不变量（红线）
# ---------------------------------------------------------------------------

class TestStorageInvariants:
    def test_rule_never_forgotten(self, tmp_path: Path) -> None:
        store = SQLiteMemoryStore(db_path=str(tmp_path / "r.db"), embedding_dim=8)
        mem = MemoryInstance(
            scope="s", content="永远不要删除生产数据", mem_type=MemoryType.RULE,
            no_decay=True, content_hash=content_hash("s", "永远不要删除生产数据"),
        )
        assert store.add(mem) is True
        with pytest.raises(RuleImmortalError):
            store.forget(mem.id, "s")
        assert store.get(mem.id, "s").status.value == "ACTIVE"

    def test_rule_supersede_allowed_but_traced(self, tmp_path: Path) -> None:
        """supersede 是治理更新允许作用于 RULE；旧规则保留可追溯（维度 C）。"""
        store = SQLiteMemoryStore(db_path=str(tmp_path / "r2.db"), embedding_dim=8)
        old = MemoryInstance(scope="s", content="必须用 tab 缩进",
                             mem_type=MemoryType.RULE, no_decay=True,
                             content_hash=content_hash("s", "必须用 tab 缩进"))
        new = MemoryInstance(scope="s", content="必须用空格缩进",
                             mem_type=MemoryType.RULE, no_decay=True,
                             content_hash=content_hash("s", "必须用空格缩进"))
        store.add(old)
        store.add(new)
        store.supersede(old.id, new.id, "s")
        got = store.get(old.id, "s")
        assert got.status.value == "SUPERSEDED" and got.superseded_by == new.id
