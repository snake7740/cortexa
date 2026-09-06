"""A2 本地回放器测试：纯分桶逻辑 + 端到端自检。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples"))

import aml_replay  # noqa: E402


def test_classify_evidence_pure() -> None:
    cls = aml_replay.classify_evidence
    # not-in-corpus
    assert cls(None, [], {})[0] == "not-in-corpus"
    # hit
    raw = {"fts": {"a"}, "vec": set()}
    assert cls("a", ["a"], raw)[0] == "hit"
    # fusion-miss：通道找到但未进 top_k
    assert cls("a", ["b"], raw)[0] == "fusion-miss"
    # channel-miss：任何通道都没找到
    assert cls("a", ["b"], {"fts": {"b"}, "vec": set()})[0] == "channel-miss"


def test_selfcheck_end_to_end(tmp_path: Path) -> None:
    report = aml_replay.run_replay_convs(
        aml_replay.SELFCHECK_CONVS, out_dir=tmp_path / "out", top_k=5
    )
    totals = report["totals"]
    assert totals["questions"] == 2
    # q1 证据在库且应命中；q2 证据不在库 → not-in-corpus
    assert totals["buckets"].get("hit", 0) >= 1
    assert totals["buckets"].get("not-in-corpus", 0) == 1
    assert report["meta"]["conversations"] == 1
    # 输出文件落盘（json + md）
    outs = [p.name for p in (tmp_path / "out").iterdir()]
    assert any(n.startswith("aml_replay_") and n.endswith(".json") for n in outs)
    assert any(n.startswith("aml_replay_") and n.endswith(".md") for n in outs)
    # 所有 bucket 值合法
    allowed = set(aml_replay.BUCKETS)
    for q in report["questions"]:
        for e in q["evidence"]:
            assert e["bucket"] in allowed, e
