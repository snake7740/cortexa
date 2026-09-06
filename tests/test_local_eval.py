"""本地评测模拟器冒烟锁（w3_simulator）：CLI 端到端可跑 + 报告表结构完整。

只锁"工具可用"（进程退出码/报告键齐全/无退化硬下限），不锁具体分数——
分数随内置题集与管线演进，写死会让回归锁变成维护负担。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """对称编码跑子进程：子进程输出含中文，Windows 管道默认 GBK，
    读端默认解码会 UnicodeDecodeError（stdout 变 None）。两端锁死 UTF-8。

    PYTHONPATH 自足：不依赖外部环境碰巧携带（IDE 通道无 PYTHONPATH，
    2026-09-01 实锤）——子进程必须自己找得到项目根。
    """
    root = str(_EXAMPLES.parent)
    existing = os.environ.get("PYTHONPATH", "")
    env = dict(
        os.environ,
        PYTHONIOENCODING="utf-8",
        PYTHONPATH=f"{root}{os.pathsep}{existing}" if existing else root,
    )
    return subprocess.run(
        [sys.executable, str(_EXAMPLES / "local_eval.py"), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=300,
    )


def test_local_eval_cli_runs_and_reports() -> None:
    proc = _run(["--top-k", "5"])
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = proc.stdout
    # 消融两档都在（通道去留裁判的对照必须存在）
    assert "entity_channel=on" in out and "entity_channel=off" in out
    # 报告键齐全
    for key in ("hit@1", "hit@5", "mrr", "unanswerable_avg_results",
                "add_p95_ms", "search_p95_ms"):
        assert key in out, f"报告缺键 {key}"


def test_local_eval_hit5_not_below_floor() -> None:
    """无退化硬下限：内置题集上 Hit@5 不得低于 0.9。

    任何检索侧改动（通道/排序/截断）把模拟器打到 0.9 以下 = 回归，先修再合。
    """
    proc = _run(["--no-ablation"])
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(ln for ln in proc.stdout.splitlines() if "entity_channel=on" in ln)
    hit5 = float(line.split(" | ")[2])
    assert hit5 >= 0.9, f"Hit@5={hit5} 跌破 0.9 下限——检索回归"


def test_local_eval_governance_dimension_full_green() -> None:
    """维度 D 治理评测必须全绿（状态级语义，非调参产物）。

    3 正向 supersede 用例（currently/no longer/prefer over）+ 1 防过度对照：
    治理词命中且内容同主题 -> 旧记忆 SUPERSEDED + superseded_by 指向新 + 新可检索；
    补充性陈述（同 subject 不同属性）-> 不得误杀。任一失败 = 维度 D 回归。
    """
    proc = _run(["--no-ablation"])
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(ln for ln in proc.stdout.splitlines() if "entity_channel=on" in ln)
    cols = dict(zip(
        [c.strip() for c in proc.stdout.splitlines()[
            proc.stdout.splitlines().index(line) - 1].split("|")],
        [c.strip() for c in line.split("|")],
    ))
    assert cols["gov_supersede"] == "3/3", f"治理 supersede 未全绿: {cols['gov_supersede']}"
    assert cols["gov_no_overkill"] == "1/1", f"防过度对照未全绿: {cols['gov_no_overkill']}"
    # 维度 B/C/F 全绿（词元确定性命中，非调参产物——检索改动致缺召回即回归）
    for d, expect in (("dim_B", "2/2"), ("dim_C", "2/2"), ("dim_F", "2/2")):
        assert cols[d] == expect, f"维度 {d} 未全绿: {cols[d]}"