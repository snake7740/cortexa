"""A2 本地回放器测试：纯分桶逻辑 + 端到端自检 + A3 provider 参数面。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples"))

import aml_replay  # noqa: E402

from cortexa.config import Settings  # noqa: E402
from cortexa.llm import LLMClient  # noqa: E402


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


def test_report_meta_carries_provider_label(tmp_path: Path) -> None:
    """A3：成对实验档位（provider/embedding_dim/label）必须落进报告元数据。"""
    report = aml_replay.run_replay_convs(
        aml_replay.SELFCHECK_CONVS, out_dir=tmp_path / "o2", top_k=5,
        provider="hash", embedding_dim=512, label="ab-hash",
    )
    meta = report["meta"]
    assert meta["provider"] == "hash"
    assert meta["embedding_dim"] == 512
    assert meta["label"] == "ab-hash"


def test_minilm_provider_dim_guard_offline() -> None:
    """A3：minilm 在维度 != 384 时必须显式失败（不静默降级 hash）。"""
    cfg = Settings(embedding_provider="minilm", embedding_dim=512, _env_file=None)
    with pytest.raises(RuntimeError, match="384"):
        LLMClient(cfg).embed(["hello world"])


def test_packaging_reporting(tmp_path: Path) -> None:
    """Q8：--packaging 不改 membership（raw 与 pm1 buckets 一致），只增送答文本量。"""
    raw = aml_replay.run_replay_convs(
        aml_replay.SELFCHECK_CONVS, out_dir=tmp_path / "pr", top_k=2, packaging="raw"
    )
    pm1 = aml_replay.run_replay_convs(
        aml_replay.SELFCHECK_CONVS, out_dir=tmp_path / "pp", top_k=2, packaging="pm1"
    )
    assert raw["totals"]["buckets"] == pm1["totals"]["buckets"]  # membership 不变
    assert raw["meta"]["packaging"] == "raw"
    assert pm1["meta"]["packaging"] == "pm1"
    assert (pm1["totals"]["avg_tokens_per_query_packaged"]
            >= pm1["totals"]["avg_tokens_per_query_base"])
    assert pm1["totals"]["packaging_chars"] >= 0
    for q in pm1["questions"]:
        assert "packaging" in q and "tokens_returned_packaged" in q


def test_expand_and_token_accounting(tmp_path: Path) -> None:
    """B1/B5：--expand 信号触发不崩溃；totals 带 tokens/query 与 expand 统计。"""
    report = aml_replay.run_replay_convs(
        aml_replay.SELFCHECK_CONVS, out_dir=tmp_path / "o3", top_k=2,
        expand=True,
    )
    totals = report["totals"]
    assert totals["avg_tokens_per_query_base"] >= 0
    assert totals["avg_tokens_per_query_full"] >= totals["avg_tokens_per_query_base"]
    assert totals["expand"]["enabled"] is True
    assert totals["expand"]["triggered"] >= 0 and totals["expand"]["added"] >= 0
    for q in report["questions"]:
        assert "tokens_returned_base" in q and "tokens_returned_full" in q
        assert "expand" in q
