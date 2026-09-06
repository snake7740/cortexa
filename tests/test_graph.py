"""实体关系图测试（Palantir Link type 建模纪律回归锁，2026-08-31）。

业务锚点：
- 单表双向：一条边两侧都可遍历，不建反向表；
- link_type 必选维度：同两实体可有多条语义独立的边（叶长生↔苏晚晴
  可同时是"同行"与"因果羁绊"）；
- 基数守卫：受限端冲突时不给替代来源则拒写（宁可拒写不可脏写），
  replace 取代旧边必须留痕（superseded_by 指向新边）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortexa import Cortexa
from cortexa.modules.graph import CardinalityError


@pytest.fixture()
def cm(tmp_path: Path) -> Cortexa:
    with Cortexa(data_dir=tmp_path / "lib") as facade:
        yield facade


class TestBidirectional:
    """单表双向：从任一侧遍历都能见到这条边。"""

    def test_both_sides_traverse(self, cm: Cortexa) -> None:
        s = "novel"
        cm.link(s, "叶长生", "苏晚晴", "同行", evidence="Ch109 结伴北上")
        from_src = cm.neighbors(s, "叶长生")
        from_dst = cm.neighbors(s, "苏晚晴")
        assert [other for other, _ in from_src] == ["苏晚晴"]
        assert [other for other, _ in from_dst] == ["叶长生"]
        assert from_src[0][1].evidence == "Ch109 结伴北上"

    def test_multi_link_types_between_same_pair(self, cm: Cortexa) -> None:
        """同两实体多条语义独立的边（官方：Flight↔Aircraft 执飞+维修记录同存）。"""
        s = "novel"
        cm.link(s, "叶长生", "苏晚晴", "同行", evidence_chapter=109)
        cm.link(s, "叶长生", "苏晚晴", "因果羁绊", evidence_chapter=112)
        cm.link(s, "叶长生", "苏晚晴", "传承线", evidence_chapter=112)
        types = {row.link_type for _, row in cm.neighbors(s, "叶长生")}
        assert types == {"同行", "因果羁绊", "传承线"}
        # 按类型过滤只取单语义
        only = cm.neighbors(s, "苏晚晴", link_type="因果羁绊")
        assert [row.link_type for _, row in only] == ["因果羁绊"]


class TestCardinality:
    """基数声明 + 违例治理：拒写优先，取代必须留痕。"""

    def test_one_to_many_violation_rejected(self, cm: Cortexa) -> None:
        s = "novel"
        cm.declare_link_type(s, "亲生父亲", "1-m")  # 一人至多一个亲生父亲
        cm.link(s, "叶长生", "叶巨川", "亲生父亲", evidence_chapter=117)
        with pytest.raises(CardinalityError):
            cm.link(s, "叶长生", "别人", "亲生父亲")
        # 拒写不落库：活跃边仍只有一条
        assert len(cm.neighbors(s, "叶长生", link_type="亲生父亲")) == 1

    def test_replace_supersedes_with_trace(self, cm: Cortexa) -> None:
        s = "novel"
        cm.declare_link_type(s, "亲生父亲", "1-m")
        cm.link(s, "叶长生", "叶巨川", "亲生父亲", evidence_chapter=117)
        new_id = cm.link(s, "叶长生", "叶巨川", "亲生父亲",
                         evidence="Ch120 更正", replace=True)
        active = cm.neighbors(s, "叶长生", link_type="亲生父亲")
        assert len(active) == 1 and active[0][1].id == new_id
        # 历史可追溯：旧边 SUPERSEDED 且指向新边
        history = cm.link_history(s, "叶长生")
        old = next(r for r in history if r.id != new_id)
        assert old.status == "SUPERSEDED" and old.superseded_by == new_id

    def test_one_to_one_conflict_on_either_side(self, cm: Cortexa) -> None:
        s = "novel"
        cm.declare_link_type(s, "道侣", "1-1")
        cm.link(s, "叶长生", "苏晚晴", "道侣")
        with pytest.raises(CardinalityError):
            cm.link(s, "叶长生", "旁人", "道侣")  # src 端冲突
        with pytest.raises(CardinalityError):
            cm.link(s, "旁人", "苏晚晴", "道侣")  # dst 端冲突

    def test_many_to_one_direction_normalized(self, cm: Cortexa) -> None:
        """m-1：dst 端受限。link(师父, 弟子) —— 每弟子至多一位授业师父，
        同一师父可收多弟子（受限端归一化后守卫统一查存储 src 端）。"""
        s = "novel"
        cm.declare_link_type(s, "授业师父", "m-1")
        cm.link(s, "孟知秋", "苏晚晴", "授业师父")
        # 同一弟子再来一位师父 -> dst 端冲突；另一弟子连同一师父 -> 允许
        with pytest.raises(CardinalityError):
            cm.link(s, "别人", "苏晚晴", "授业师父")
        cm.link(s, "孟知秋", "叶长生", "授业师父")
        assert len(cm.neighbors(s, "孟知秋", link_type="授业师父")) == 2

    def test_undeclared_type_auto_registers_mm(self, cm: Cortexa) -> None:
        s = "novel"
        cm.link(s, "旧派", "天机阁", "分裂自")
        assert cm.link_types(s)["分裂自"] == "m-m"


class TestLifecycle:
    """边生命周期：删除留痕，历史可查。"""

    def test_unlink_removes_from_active_keeps_history(self, cm: Cortexa) -> None:
        s = "novel"
        link_id = cm.link(s, "叶长生", "沈渊", "传承")
        assert cm.unlink(s, link_id) is True
        assert cm.neighbors(s, "叶长生") == []
        history = cm.link_history(s, "叶长生")
        assert history[0].status == "REMOVED"
        # 重复删不命中
        assert cm.unlink(s, link_id) is False
