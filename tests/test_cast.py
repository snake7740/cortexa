"""CastRegistry 单测：别名消解（图分析实证案例）。"""

from cortexa.domain import CastRegistry
from cortexa.schema import get_schema


def _reg():
    return CastRegistry(get_schema("novel"))


def test_canonicalize_aliases():
    r = _reg()
    assert r.canonicalize("长生") == "叶长生"
    assert r.canonicalize("铁柱") == "赵铁柱"
    assert r.canonicalize("晚晴") == "苏晚晴"


def test_canonical_passthrough():
    r = _reg()
    assert r.canonicalize("叶长生") == "叶长生"  # 主名不变
    assert r.canonicalize("路人甲") == "路人甲"  # 非别名不变


def test_is_alias():
    r = _reg()
    assert r.is_alias("长生")
    assert not r.is_alias("叶长生")


def test_all_names_longest_first():
    r = _reg()
    names = r.all_names()
    assert "叶长生" in names and "长生" in names
    # 长名在前（防"叶长生"被"长生"前缀误匹配）
    assert names.index("叶长生") < names.index("长生")


def test_counts_merge_via_canonical():
    """图分析核心诉求：长生/叶长生 合并计数。"""
    r = _reg()
    mentions = ["长生", "叶长生", "长生", "叶长生", "叶长生"]
    merged = {}
    for m in mentions:
        c = r.canonicalize(m)
        merged[c] = merged.get(c, 0) + 1
    assert merged["叶长生"] == 5  # 全部归一
