"""A1 检索质量回归门测试：默认档必须通过（回退即红），基线文件存在且含全部键。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import retrieval_gate  # noqa: E402


def test_baseline_file_present_and_keys() -> None:
    metrics = retrieval_gate.load_baseline()
    for key in ("hit@1", "hit@5", "mrr",
                "gov_supersede_ratio", "gov_no_overkill_ratio",
                "dim_B_ratio", "dim_C_ratio", "dim_F_ratio"):
        assert key in metrics, key


def test_current_retrieval_passes_gate() -> None:
    """当前代码默认档不得低于基线 2%（门禁自身先绿）。"""
    ok, cur, failures = retrieval_gate.evaluate()
    assert ok, failures
    assert cur["hit@1"] >= 0.8  # 题集小但不得低于 0.8 的绝对底线
