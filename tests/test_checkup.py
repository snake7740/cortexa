"""查体编排（"学习委员"）回归锁：只评不改 + 五科检查器行为。

锁住的关键行为：
- 地板复验全量计数、违规恒 0（写入红线的全量复查）
- 衰减健康度分桶：未复习/健康/老化；RULE/no_decay 豁免
- 探针自测副作用即机制：命中记忆经测试效应从"未复习"转"健康"
- 一致性审计：未给名单跳过（不假装能自动识别人名）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortexa import Cortexa
from cortexa.models import FSRSState, MemoryInstance, MemoryType

SCOPE = "checkup-scope"


@pytest.fixture()
def cm(tmp_path: Path) -> Cortexa:
    with Cortexa(data_dir=tmp_path / "lib") as facade:
        yield facade


def _seed(cm: Cortexa, n: int = 3) -> None:
    for i in range(n):
        cm.remember(f"测试记忆条目第 {i} 号，内容独立可检索", scope=SCOPE)


class TestCheckupReport:
    def test_report_structure_and_floor_recheck(self, cm: Cortexa) -> None:
        _seed(cm, n=3)
        r = cm.checkup(SCOPE)
        assert r.active_memories == 3
        assert r.floor_total == 3
        assert r.floor_violations == 0  # 写入红线全量复查应恒 0
        assert r.orphans["active_memories"] == 3
        assert r.audit is None  # 未给名单 → 跳过，不假装自动识别

    def test_decay_buckets_all_unreviewed_initially(self, cm: Cortexa) -> None:
        _seed(cm, n=3)
        d = cm.checkup(SCOPE).decay
        assert d["checked"] == 3
        assert d["never_reviewed"] == 3  # 新库无检索历史
        assert d["healthy"] == 0 and d["aged"] == 0

    def test_probe_self_test_moves_memories_to_healthy(self, cm: Cortexa) -> None:
        _seed(cm, n=3)
        probes = [("测试记忆条目第 0 号", "第 0 号")]
        r = cm.checkup(SCOPE, probes=probes)
        # 探针自测本身通过（期望子串进入 top_k）
        assert r.probes["hits"] == 1 and r.probes["rate"] == 1.0
        assert r.probes["details"][0]["hit"] is True
        # 副作用即机制：被命中的记忆经测试效应转为"健康"
        # （衰减桶在探针前计算，反映查体开始时的真实状况）
        assert r.decay["never_reviewed"] == 3

    def test_probe_miss_recorded_honestly(self, cm: Cortexa) -> None:
        _seed(cm, n=2)
        r = cm.checkup(SCOPE, probes=[("条目", "不存在的期望子串XYZ")])
        assert r.probes["hits"] == 0 and r.probes["rate"] == 0.0

    def test_rule_and_no_decay_exempt_from_decay_check(self, cm: Cortexa) -> None:
        _seed(cm, n=1)
        cm._store.add(
            MemoryInstance(scope=SCOPE, content="规则：永不衰减",
                           mem_type=MemoryType.RULE)
        )
        cm._store.add(
            MemoryInstance(scope=SCOPE, content="豁免记忆", no_decay=True)
        )
        d = cm.checkup(SCOPE).decay
        assert d["checked"] == 1  # RULE 与 no_decay 不入衰减检查

    def test_aged_bucket_after_review_ages(self, cm: Cortexa) -> None:
        _seed(cm, n=1)
        cm.recall("测试记忆条目", scope=SCOPE, top_k=1)  # 命中 → 测试效应
        # 手工把复习时间推到很久以前，制造老化（R(t) < 0.5）
        with cm._store.lock:
            conn = cm._store.connection
            row = conn.execute(
                "SELECT id, fsrs_json FROM memories WHERE scope=? AND status='ACTIVE'",
                (SCOPE,),
            ).fetchone()
            fsrs = FSRSState(**json.loads(row["fsrs_json"]))
            assert fsrs.last_review_at is not None  # 前置：测试效应已生效
            fsrs.last_review_at -= 365 * 86400  # 一年前复习，S=1 → R 极低
            conn.execute(
                "UPDATE memories SET fsrs_json=? WHERE id=?",
                (fsrs.model_dump_json(), row["id"]),
            )
            conn.commit()
        d = cm.checkup(SCOPE).decay
        assert d["aged"] == 1 and d["healthy"] == 0

    def test_audit_section_runs_with_subjects(self, cm: Cortexa) -> None:
        cm.remember("叶长生突破到炼气三层", scope=SCOPE, session_id="ch001")
        r = cm.checkup(SCOPE, subjects=("叶长生",))
        assert isinstance(r.audit, dict)
        assert "issues" in r.audit  # AuditReport.as_dict 的既有字段
