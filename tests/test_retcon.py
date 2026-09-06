"""retcon 修正写入测试（Palantir Action type 移植回归锁，2026-08-31）。

业务锚点（双闭环实战教训）：
- 修正已入库内容必须走 op_type='retcon' + 强制 supersede 链，禁止静默改写；
- retcon 后审计不得再见旧值（status='ACTIVE' 口径，retcon 旧正文不再触发异常）；
- 新记忆继承旧记忆的事件时间（逻辑时间轴不漂移，时点切片仍见修正后内容）；
- 找不到活跃旧记忆 / 新旧内容相同 -> 拒写（宁可报错不可脏写）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortexa import Cortexa
from cortexa.modules.consistency import DEFAULT_SCALE, slice_at

T0 = 1_760_000_000_000
INTERVAL = 3_600_000


@pytest.fixture()
def cm(tmp_path: Path) -> Cortexa:
    with Cortexa(data_dir=tmp_path / "lib") as facade:
        yield facade


def _ts(ch: int) -> int:
    return T0 + ch * INTERVAL


class TestRetconChain:
    """强制取代链：旧记忆过期留痕，新记忆带 retcon 标记。"""

    def test_chain_fields_and_status(self, cm: Cortexa) -> None:
        s = "novel"
        old_text = "【第74章·】孟知秋是苏晚晴的师父。"
        cm.remember(old_text, scope=s, session_id="ch074", timestamp=_ts(74))
        result = cm.retcon(s, old_text, "【第74章·】孟知秋是苏晚晴的曾祖母。",
                           subject="孟知秋", reason="Ch112 正文确认辈分")
        old = cm._store.get(result["old_id"], s)
        new = cm._store.get(result["new_id"], s)
        assert old is not None and old.status.value == "SUPERSEDED"
        assert old.superseded_by == result["new_id"]
        assert new is not None and new.status.value == "ACTIVE"
        assert new.op_type == "retcon" and new.supersedes_id == result["old_id"]
        # 继承旧记忆的会话与事件时间（时间轴不漂移）
        assert new.session_id == "ch074" and new.event_ts_ms == _ts(74)
        assert result["chapter"] == 74

    def test_double_retcon_forbidden(self, cm: Cortexa) -> None:
        """已被取代的记忆不可再次作为修正对象（避免套套链）。"""
        s = "novel"
        old_text = "【第3章·】旧设定：灵根分五行。"
        cm.remember(old_text, scope=s, session_id="ch003", timestamp=_ts(3))
        cm.retcon(s, old_text, "【第3章·】新设定：灵根分五行加变异。")
        with pytest.raises(KeyError):
            cm.retcon(s, old_text, "【第3章·】再次修正。")


class TestRetconGuards:
    """拒写守卫：定位不到对象或内容无效时报错，不落脏数据。"""

    def test_missing_old_memory_rejected(self, cm: Cortexa) -> None:
        with pytest.raises(KeyError):
            cm.retcon("novel", "不存在的旧内容", "新内容")

    def test_identical_content_rejected(self, cm: Cortexa) -> None:
        s = "novel"
        text = "【第5章·】虎符是黑铁半符。"
        cm.remember(text, scope=s, session_id="ch005", timestamp=_ts(5))
        with pytest.raises(ValueError):
            cm.retcon(s, text, text)
        # 拒写后原记忆不受影响
        assert cm.stats(s)["active_memories"] == 1

    def test_scope_isolation(self, cm: Cortexa) -> None:
        """同名内容在别的 scope 不构成修正对象。"""
        text = "【第5章·】虎符是黑铁半符。"
        cm.remember(text, scope="novel", session_id="ch005", timestamp=_ts(5))
        with pytest.raises(KeyError):
            cm.retcon("other-scope", text, "改写")


class TestAuditScope:
    """审计口径：retcon 后，旧正文不再进入状态线/异常清单。"""

    def test_audit_ignores_superseded(self, cm: Cortexa) -> None:
        s = "novel"
        cm.remember("【第3章·】叶长生的修为是炼气三层。",
                    scope=s, session_id="ch003", timestamp=_ts(3))
        cm.remember("【第7章·】叶长生突破到炼气五层。",
                    scope=s, session_id="ch007", timestamp=_ts(7))
        # 修正 Ch7 为一个"回跳"值：若旧值仍在库，审计会报 regression
        cm.retcon(s, "【第7章·】叶长生突破到炼气五层。",
                  "【第7章·】叶长生的修为是炼气四层。", subject="叶长生")
        report = cm.audit(s, subjects=("叶长生",))
        line = report.state_lines["叶长生"]
        assert [p.value for p in line] == ["炼气三层", "炼气四层"]
        assert [p.confidence for p in line] == ["high", "high"]

    def test_slice_at_sees_corrected_value(self, cm: Cortexa) -> None:
        """时点切片：修正继承事件时间，Ch7 时点见修正后值。"""
        s = "novel"
        cm.remember("【第3章·】叶长生的修为是炼气三层。",
                    scope=s, session_id="ch003", timestamp=_ts(3))
        cm.remember("【第7章·】叶长生突破到筑基初期。",
                    scope=s, session_id="ch007", timestamp=_ts(7))
        cm.retcon(s, "【第7章·】叶长生突破到筑基初期。",
                  "【第7章·】叶长生的修为是炼气五层。", subject="叶长生")
        points = slice_at(cm._store.connection, s, 7,
                          scale=DEFAULT_SCALE, subject="叶长生")
        assert points and points[0].value == "炼气五层"


class TestEntityHistory:
    """编辑史三源合流：状态线 + retcon 事件 + 边史。"""

    def test_history_merges_three_sources(self, cm: Cortexa) -> None:
        s = "novel"
        cm.remember("【第3章·】叶长生的修为是炼气三层。",
                    scope=s, session_id="ch003", timestamp=_ts(3))
        cm.remember("【第7章·】叶长生的修为是炼气五层。",
                    scope=s, session_id="ch007", timestamp=_ts(7))
        cm.retcon(s, "【第7章·】叶长生的修为是炼气五层。",
                  "【第7章·】叶长生的修为是炼气四层。",
                  subject="叶长生", reason="笔误修正")
        cm.link(s, "叶长生", "叶巨川", "亲生父亲", evidence_chapter=117)

        hist = cm.entity_history(s, "叶长生")
        assert [p["value"] for p in hist["state_line"]] == ["炼气三层", "炼气四层"]
        assert len(hist["retcons"]) == 1
        assert hist["retcons"][0]["chapter"] == 7
        assert hist["retcons"][0]["reason"] == "笔误修正"
        assert [ln["link_type"] for ln in hist["links"]] == ["亲生父亲"]

    def test_retcon_filtered_by_subject(self, cm: Cortexa) -> None:
        s = "novel"
        cm.remember("【第3章·】苏晚晴的修为是炼气二层。",
                    scope=s, session_id="ch003", timestamp=_ts(3))
        cm.retcon(s, "【第3章·】苏晚晴的修为是炼气二层。",
                  "【第3章·】苏晚晴的修为是炼气三层。", subject="苏晚晴")
        assert cm.entity_history(s, "叶长生")["retcons"] == []
        assert len(cm.entity_history(s, "苏晚晴")["retcons"]) == 1