"""一致性审计测试：实体归属防污染 / 回跳与跳级 / 物品缺口 / low 置信兜底。

业务锚点（2026-08-31 原型实验教训回归锁）：
- "条目含主角名但修为说的是别人"的 24 处误报 -> 同句+状态动词归属规则；
- 作者工具"不知道谁的修为" -> subjects 由调用方提供；
- 章号双源解析（session_id chNNN 优先，内容【第N章·】兜底）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortexa import Cortexa
from cortexa.modules.consistency import (
    DEFAULT_SCALE,
    build_state_line,
    check_item_gap,
    check_state_line,
)


@pytest.fixture()
def cm(tmp_path: Path) -> Cortexa:
    with Cortexa(data_dir=tmp_path / "lib") as facade:
        yield facade


def _remember_chapter(cm: Cortexa, scope: str, ch: int, content: str) -> None:
    cm.remember(content, scope=scope, session_id=f"ch{ch:03d}", timestamp=1_760_000_000_000 + ch * 3_600_000)


def _rows(cm: Cortexa, scope: str) -> list[tuple[str, str, str]]:
    conn = cm._store.connection
    rows = conn.execute(
        "SELECT id, session_id, content FROM memories WHERE scope = ?", (scope,)
    ).fetchall()
    return [(r["id"], r["session_id"], r["content"]) for r in rows]


class TestEntityAttribution:
    """归属防污染：他人的境界不得进入主角状态线（原型实验 24 处误报的回归锁）。"""

    def test_other_character_realm_not_attributed(self, cm: Cortexa) -> None:
        s = "novel"
        _remember_chapter(cm, s, 3, "【第3章·启程】叶长生的修为是炼气三层。")
        # Ch5 提到更高境界，但主语是赵长老，且与叶长生不同句
        _remember_chapter(
            cm, s, 5, "【第5章·试炼】赵长老已经是筑基中期。叶长生冷眼旁观。"
        )
        _remember_chapter(cm, s, 7, "【第7章·突破】叶长生突破到炼气五层。")
        line = build_state_line(_rows(cm, s), "叶长生", DEFAULT_SCALE)
        assert [p.value for p in line] == ["炼气三层", "炼气五层"]
        assert all(p.confidence == "high" for p in line)

    def test_same_sentence_no_verb_is_low_confidence(self, cm: Cortexa) -> None:
        s = "novel"
        _remember_chapter(cm, s, 3, "【第3章·】叶长生炼气三层，站在门口。")
        line = build_state_line(_rows(cm, s), "叶长生", DEFAULT_SCALE)
        assert len(line) == 1 and line[0].confidence == "low"

    def test_low_fallback_when_no_high_in_chapter(self, cm: Cortexa) -> None:
        s = "novel"
        _remember_chapter(cm, s, 2, "【第2章·】叶长生的修为是炼气二层。")
        _remember_chapter(cm, s, 4, "【第4章·】众人看着叶长生炼气四层的背影。")
        line = build_state_line(_rows(cm, s), "叶长生", DEFAULT_SCALE)
        assert [p.value for p in line] == ["炼气二层", "炼气四层"]
        assert line[0].confidence == "high" and line[1].confidence == "low"


class TestSameSentenceMultiPerson:
    """同句多人消歧（2026-08-31 audit 复跑暴露的三处取词歧义回归锁）。"""

    def test_semicolon_list_isolates_subjects(self, cm: Cortexa) -> None:
        # Ch37 实案：分号清单中秦洛=八层，不得取到周暑的七层
        _remember_chapter(cm, "n", 37, "【第37章·】种子轮弟子：秦洛，炼气八层巅峰；周暑，炼气七层。")
        line = build_state_line(_rows(cm, "n"), "秦洛", DEFAULT_SCALE)
        assert len(line) == 1 and line[0].value == "炼气八层"

    def test_first_realm_after_subject_wins(self, cm: Cortexa) -> None:
        # Ch93 实案：叶长生=九层（主语子句），不得取到后面的"他是炼气八层"
        _remember_chapter(cm, "n", 93, "【第93章·】叶长生是炼气九层，他是炼气八层。")
        line = build_state_line(_rows(cm, "n"), "叶长生", DEFAULT_SCALE)
        assert line[0].value == "炼气九层"

    def test_cross_clause_attribution_downgraded_to_low(self, cm: Cortexa) -> None:
        # Ch31 实案：境界词在后续子句（说的是"这个老人"）-> 归属降 low
        _remember_chapter(
            cm, "n", 31,
            "【第31章·】叶长生从因果之眼的余光里瞥到灵力波动，这个老人至少有炼气九层的修为。",
        )
        line = build_state_line(_rows(cm, "n"), "叶长生", DEFAULT_SCALE)
        assert len(line) == 1 and line[0].value == "炼气九层" and line[0].confidence == "low"

    def test_segment_without_realm_not_attributed(self, cm: Cortexa) -> None:
        # 主语段无任何等级词 -> 不记点（不凭空造点）。
        # 已知边界：同段后续子句若含等级词（如"叶长生站在山门外，周元白已是炼气三层"）
        # 会跨子句归属但降 low —— 语义级归属需 LLM，见模块诚实声明。
        _remember_chapter(cm, "n", 1, "【第1章·】叶长生冷眼旁观，转身离去。周元白已是炼气三层。")
        line = build_state_line(_rows(cm, "n"), "叶长生", DEFAULT_SCALE)
        assert line == []


class TestIssueDetection:
    def test_regression_detected(self, cm: Cortexa) -> None:
        s = "novel"
        _remember_chapter(cm, s, 7, "【第7章·】叶长生突破到炼气五层。")
        _remember_chapter(cm, s, 9, "【第9章·】叶长生的修为只有炼气三层了。")
        line = build_state_line(_rows(cm, s), "叶长生", DEFAULT_SCALE)
        issues = check_state_line(line, DEFAULT_SCALE)
        assert any(i.kind == "regression" and "Ch7 -> Ch9" in i.between for i in issues)

    def test_jump_detected(self, cm: Cortexa) -> None:
        s = "novel"
        _remember_chapter(cm, s, 12, "【第12章·】叶长生的修为是炼气三层。")
        _remember_chapter(cm, s, 13, "【第13章·】叶长生晋升炼气九层。")
        line = build_state_line(_rows(cm, s), "叶长生", DEFAULT_SCALE)
        issues = check_state_line(line, DEFAULT_SCALE)
        assert any(i.kind == "jump" and "+6" in i.detail for i in issues)

    def test_item_gap_detected(self, cm: Cortexa) -> None:
        s = "novel"
        _remember_chapter(cm, s, 9, "【第9章·】黑色木牌在他掌心发烫。")
        _remember_chapter(cm, s, 10, "【第10章·】黑色木牌的字迹又深了一分。")
        _remember_chapter(cm, s, 15, "【第15章·】他把黑色木牌翻了个面。")
        issues = check_item_gap(_rows(cm, s), "黑色木牌")
        assert len(issues) == 1
        assert "Ch10 -> Ch15" in issues[0].between and "缺席" in issues[0].detail


class TestRetroFix4:
    """复盘 #4 回归锁（2026-09-01）：等级表覆盖 / 模糊修饰降级 / high-only 检测 / 物品别名。"""

    def test_peak_realm_extracted(self, cm: Cortexa) -> None:
        # 复盘实证：状态线停在 Ch102 因"炼气九层巅峰"不在等级表。
        _remember_chapter(cm, "n", 102, "【第102章·】叶长生现在的修为是炼气九层巅峰。")
        line = build_state_line(_rows(cm, "n"), "叶长生", DEFAULT_SCALE)
        assert len(line) == 1 and line[0].value == "炼气九层巅峰" and line[0].confidence == "high"
        # 巅峰数值同基阶：巅峰->筑基初期 不算跳级（+1）
        _remember_chapter(cm, "n", 130, "【第130章·】叶长生突破筑基初期。")
        issues = check_state_line(build_state_line(_rows(cm, "n"), "叶长生", DEFAULT_SCALE), DEFAULT_SCALE)
        assert issues == []

    def test_hedged_realm_downgraded(self, cm: Cortexa) -> None:
        # 后缀"以上"与前缀"最多"都是模糊陈述，不得进 high 判定。
        _remember_chapter(cm, "n", 7, "【第7章·】叶长生听到了——修为到了炼气四层以上。")
        _remember_chapter(cm, "n", 40, "【第40章·】叶长生判断这老人最多炼气八层。")
        line = build_state_line(_rows(cm, "n"), "叶长生", DEFAULT_SCALE)
        assert [p.confidence for p in line] == ["low", "low"]

    def test_low_points_do_not_create_issues(self, cm: Cortexa) -> None:
        # 异常检测只对 high 点比对：low 点大起落不产生假异常。
        _remember_chapter(cm, "n", 2, "【第2章·】众人看着叶长生炼气二层的背影。")
        _remember_chapter(cm, "n", 9, "【第9章·】众人看着叶长生炼气九层的背影。")
        line = build_state_line(_rows(cm, "n"), "叶长生", DEFAULT_SCALE)
        assert all(p.confidence == "low" for p in line)
        assert check_state_line(line, DEFAULT_SCALE) == []

    def test_item_alias_fills_gap(self, cm: Cortexa) -> None:
        # 代称"戒"在场也算古虚戒在场：字面口径报假缺口，别名口径填平。
        _remember_chapter(cm, "n", 9, "【第9章·】古虚戒在他掌心发温。")
        _remember_chapter(cm, "n", 11, "【第11章·】他按了按怀里的戒。")
        _remember_chapter(cm, "n", 13, "【第13章·】戒贴着胸口。")
        _remember_chapter(cm, "n", 15, "【第15章·】古虚戒又温了一次。")
        rows = _rows(cm, "n")
        # 字面口径：只见 Ch9/Ch15 -> Ch9->Ch15 假缺口复现（回归锚点）
        issues = check_item_gap(rows, "古虚戒")
        assert len(issues) == 1 and "Ch9 -> Ch15" in issues[0].between
        # 别名口径：Ch11/Ch13 的"戒"填平在场 -> 无缺口。
        assert check_item_gap(rows, "古虚戒", aliases=("戒",)) == []
        # audit 入口的管道分隔语法同源生效。
        report = cm.audit("n", subjects=(), items=("古虚戒|戒",))
        assert report.issues == []


class TestChapterParsing:
    def test_content_prefix_fallback_without_session(self, cm: Cortexa) -> None:
        s = "novel"
        cm.remember("【第6章·回门】叶长生已是炼气六层。", scope=s)  # 无 session_id
        line = build_state_line(_rows(cm, s), "叶长生", DEFAULT_SCALE)
        assert len(line) == 1 and line[0].chapter == 6


class TestFacadeAudit:
    def test_audit_integration(self, cm: Cortexa) -> None:
        s = "novel"
        _remember_chapter(cm, s, 3, "【第3章·】叶长生的修为是炼气三层。")
        _remember_chapter(cm, s, 9, "【第9章·】叶长生突破到炼气五层。")
        _remember_chapter(cm, s, 10, "【第10章·】黑色木牌不见了。")
        _remember_chapter(cm, s, 14, "【第14章·】黑色木牌又回来了。")
        report = cm.audit(s, subjects=("叶长生",), items=("黑色木牌",))
        assert report.state_lines["叶长生"][0].value == "炼气三层"
        kinds = {i.kind for i in report.issues}
        assert kinds == {"item_gap"}  # 3->5 无跳级；只有木牌缺口
        d = report.as_dict()
        assert d["scope"] == s and len(d["issues"]) == 1
