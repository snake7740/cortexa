"""FactExtractor 单测：语境分层（NARRATION/QUOTE/RECALL）防引用误报。"""

from cortexa.domain.facts import FactExtractor, NARRATION, QUOTE, RECALL, REPORT
from cortexa.schema import get_schema


def _fx():
    return FactExtractor(get_schema("novel"))


def test_narration_is_presence():
    """作者叙述 = 活体在场。"""
    fx = _fx()
    assert fx.is_present("叶长生推开院门走了进去。", "叶长生", ["叶长生"])


def test_quote_mention_not_presence():
    """引号内被提及 = 非活体在场（赵铁柱案例：他人对话中提到）。"""
    fx = _fx()
    text = "苏晚晴说：\u201c赵铁柱死了，凶手还没找到。\u201d"
    assert not fx.is_present(text, "赵铁柱", ["苏晚晴", "赵铁柱"])


def test_recall_mention_not_presence():
    """回忆中被提及 = 非活体在场（王宣案例：叶长生回想）。"""
    fx = _fx()
    text = "叶长生想到王宣与面具长老密会时的檀香。"
    assert not fx.is_present(text, "王宣", ["叶长生", "王宣"])
    # 但叶长生本人在叙述句活体在场
    assert fx.is_present(text, "叶长生", ["叶长生", "王宣"])


def test_noun_death_reference_not_presence():
    """名词性引用：等赵铁柱的死因 = 非在场。"""
    fx = _fx()
    text = "他在青云宗等过，等赵铁柱的死因。"
    assert not fx.is_present(text, "赵铁柱", ["赵铁柱"])


def test_report_said_not_presence():
    """转述：X说Y死了 = Y 非在场（保守）。"""
    fx = _fx()
    text = "周元白说赵铁柱死了。"
    assert not fx.is_present(text, "赵铁柱", ["周元白", "赵铁柱"])


def test_ascii_quote_detected():
    """ASCII 直引号内同样非在场（正文用直引号）。"""
    fx = _fx()
    text = '苏晚晴说："赵铁柱死了。"'
    assert not fx.is_present(text, "赵铁柱", ["苏晚晴", "赵铁柱"])


def test_occurrences_classified():
    fx = _fx()
    text = "叶长生走进院门。\u201c赵铁柱死了。\u201d苏晚晴说。他想到王宣。"
    occs = fx.occurrences(text, ["叶长生", "赵铁柱", "苏晚晴", "王宣"])
    by_ent = {}
    for o in occs:
        by_ent.setdefault(o.entity, []).append(o.context)
    assert "NARRATION" in by_ent["叶长生"]
    assert by_ent["赵铁柱"] and all(c == QUOTE for c in by_ent["赵铁柱"])
    assert by_ent["王宣"] and all(c == RECALL for c in by_ent["王宣"])