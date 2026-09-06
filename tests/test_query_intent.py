"""query_intent 确定性分类测试（RAR-002，2026-09-06）。"""
from __future__ import annotations

from cortexa.modules.query_intent import classify_intent

CASES = [
    ("Where does Alice currently live?", "CURRENT"),
    ("Which city did Alice move to in 2025?", "HISTORICAL"),
    ("What did Alice do last month?", "HISTORICAL"),
    ("Did Alice live in Seattle before or after Boston?", "SEQUENCE"),
    ("Which vendors does Team Nova currently work with?", "LIST_CURRENT"),
    ("Which consultants has Team Nova used over time?", "HISTORICAL"),
    ("Where does Alice's brother work?", "DEFAULT"),
    ("Which projects is Bob currently working on?", "LIST_CURRENT"),
    ("Which project did Bob finish in 2024?", "HISTORICAL"),
    ("What are all her hobbies?", "ENUM"),
    ("Which vendor pair did Team Nova use right before the current one?", "SEQUENCE"),
    ("Where does Alice work now?", "CURRENT"),
]


def test_intent_cases() -> None:
    for q, gold in CASES:
        assert classify_intent(q)["kind"] == gold, (q, classify_intent(q))


def test_intent_deterministic() -> None:
    for q, _g in CASES:
        assert classify_intent(q) == classify_intent(q)
