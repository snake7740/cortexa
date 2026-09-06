"""LifecycleEngine 单测：schema.transitions 驱动的状态机（周元白/王宣案例锚点）。"""

import pytest

from cortexa.domain import LifecycleEngine
from cortexa.schema import SceneSchema, get_schema


@pytest.fixture
def engine():
    return LifecycleEngine(get_schema("novel"))


def test_death_narration_to_exited(engine):
    """NARRATION 语境死亡：ACTIVE -> EXITED（赵铁柱案例正确路径）。"""
    r = engine.apply_event("death", "ACTIVE", context="NARRATION")
    assert r.ok and r.to_state == "EXITED"


def test_death_quote_rejected(engine):
    """QUOTE 语境死亡被拒——防"叶长生说赵铁柱死了"误报 EXITED。"""
    r = engine.apply_event("death", "ACTIVE", context="QUOTE")
    assert not r.ok
    assert "NARRATION" in r.reason


def test_death_recall_rejected(engine):
    """RECALL（回忆）死亡被拒——王宣/方桐"死后仍被引用"不再误报。"""
    r = engine.apply_event("death", "ACTIVE", context="RECALL")
    assert not r.ok


def test_depart_to_dormant_not_exited(engine):
    """离场只到 DORMANT——周元白 64 章离场不被判死亡。"""
    r = engine.apply_event("depart", "ACTIVE", context="NARRATION")
    assert r.ok and r.to_state == "DORMANT"
    assert r.to_state != "EXITED"


def test_return_from_dormant_reintroduced(engine):
    """回归需从 DORMANT：DORMANT -> REINTRODUCED（周元白 ch105 回归）。"""
    r = engine.apply_event("return_", "DORMANT", context="NARRATION")
    assert r.ok and r.to_state == "REINTRODUCED"


def test_return_from_active_rejected(engine):
    """ACTIVE 状态不能"回归"（还在场）——防止无意义迁移。"""
    r = engine.apply_event("return_", "ACTIVE", context="NARRATION")
    assert not r.ok


def test_new_entity_always_allowed(engine):
    """新实体注册不校验迁移。"""
    r = engine.apply_event("death", None)
    assert r.ok and r.reason == "new entity"


def test_unknown_event_rejected(engine):
    r = engine.apply_event("teleport", "ACTIVE")
    assert not r.ok
    assert "未在 schema" in r.reason


def test_initial_state(engine):
    assert engine.initial_state("CHARACTER") == "INTRODUCED"
    assert engine.initial_state("ITEM") == "ACQUIRED"


def test_custom_schema_engine():
    """aml 场景自定义 schema 也能驱动同一引擎（场景无关性验证）。"""
    s = SceneSchema(
        name="aml_x",
        entity_types=("USER",),
        lifecycle_states={"USER": ("NEW", "ACTIVE", "UPDATED")},
        event_predicates={"update": ("changed", "修正")},
        transitions={"update": {"from": ("ACTIVE",), "to": "UPDATED"}},
        context_required=(),
    )
    e = LifecycleEngine(s)
    r = e.apply_event("update", "ACTIVE", context="REPORT")
    assert r.ok and r.to_state == "UPDATED"
