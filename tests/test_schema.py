"""SceneSchema 单测：加载/校验/迁移规则完整性。"""

import pytest

from cortexa.schema import (
    NOVEL_SCHEMA,
    SceneSchema,
    get_schema,
    register_schema,
)


def test_novel_schema_loads_and_validates():
    s = get_schema("novel")
    assert s.name == "novel"
    assert "CHARACTER" in s.entity_types


def test_death_transition_to_exited():
    s = get_schema("novel")
    t = s.transitions["death"]
    assert t["to"] == "EXITED"
    assert "DORMANT" in t["from"]  # 死亡可从任意存活态进入


def test_depart_not_death():
    """离场只到 DORMANT——周元白案例的架构保证。"""
    s = get_schema("novel")
    assert s.transitions["depart"]["to"] == "DORMANT"
    assert s.transitions["depart"]["to"] != "EXITED"


def test_return_requires_dormant():
    s = get_schema("novel")
    assert s.transitions["return_"]["from"] == ("DORMANT",)
    assert s.transitions["return_"]["to"] == "REINTRODUCED"


def test_death_requires_narration_context():
    """死亡事件必须 NARRATION 语境——防转述/回忆误报（王宣/方桐案例）。"""
    s = get_schema("novel")
    assert "death" in s.context_required


def test_relation_patterns_have_named_groups():
    s = get_schema("novel")
    for pat, rtype in s.relation_patterns:
        assert "?P<A>" in pat and "?P<B>" in pat


def test_invalid_schema_rejected():
    with pytest.raises(ValueError):
        bad = SceneSchema(
            name="bad",
            entity_types=("X",),
            event_predicates={"ghost_event": ("触发",)},
            transitions={"ghost_event": {"to": "NOPE"}},  # 事件未声明? 不——声明了，但 to 状态不存在
        )
        bad.validate()


def test_unknown_scene_raises():
    with pytest.raises(ValueError):
        get_schema("nonexistent")


def test_register_new_scene():
    ok = SceneSchema(
        name="aml_probe",
        entity_types=("USER", "ENTITY"),
        lifecycle_states={"USER": ("NEW", "ACTIVE", "UPDATED", "SUPERSEDED")},
        event_predicates={"update": ("changed", "updated", "修正")},
        transitions={"update": {"from": ("ACTIVE",), "to": "UPDATED"}},
    )
    register_schema(ok)
    assert get_schema("aml_probe").name == "aml_probe"