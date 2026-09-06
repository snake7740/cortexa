"""A1 检索质量回归门（2026-09-06，借鉴 brainctl 的 P@/MRR >2% 掉分即红）。

跑 local_eval 内置题集（11 可答 + 3 不可答 + 治理 + 维度 B/C/F）的默认部署档
（entity_channel=False，与参赛 Docker 同口径），把 hit@1/hit@5/mrr 与治理/维度
比率和 tests/golden/retrieval_baseline.json 基线比：任一下降 >0.02 即失败。

职责边界：只读门禁，不改任何检索代码；失败只说明"检索分回退了"，修复方式是
成对实验，不是调门槛。基线更新用 --write-baseline（改检索默认行为后重标）。

用法：
    python scripts/retrieval_gate.py             # 校验（check.py 自动调用）
    python scripts/retrieval_gate.py --write-baseline   # 重新固化基线
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
BASELINE_PATH = ROOT / "tests" / "golden" / "retrieval_baseline.json"
DROP_TOLERANCE = 0.02

sys.path.insert(0, str(EXAMPLES))
from local_eval import run_eval  # noqa: E402  # 自足：examples 非包


def _ratio(value: str) -> float:
    """'3/3' -> 1.0；异常视为 0（防漏计）。"""
    try:
        p, t = value.split("/")
        return int(p) / max(int(t), 1)
    except (ValueError, AttributeError):
        return 0.0


def _current_summary() -> dict:
    with tempfile.TemporaryDirectory(prefix="cortexa-gate-") as td:
        report = run_eval(data_dir=Path(td) / "d", entity_channel=False, top_k=5)
    return report.summary()


def _flatten(summary: dict) -> dict:
    out: dict[str, float] = {}
    for k in ("hit@1", "hit@5", "mrr"):
        out[k] = float(summary.get(k, 0.0))
    for k in ("gov_supersede", "gov_no_overkill", "dim_B", "dim_C", "dim_F"):
        out[k + "_ratio"] = _ratio(str(summary.get(k, "0/0")))
    return out


def load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        raise FileNotFoundError(
            f"基线缺失：{BASELINE_PATH}。先用 --write-baseline 固化一次。"
        )
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return data["metrics"]


def evaluate() -> tuple[bool, dict, list[str]]:
    """跑当前档并对照基线。返回 (通过?, 当前指标, 失败原因列表)。"""
    cur = _flatten(_current_summary())
    base = load_baseline()
    failures: list[str] = []
    for key, floor in base.items():
        if key not in cur:
            continue
        if cur[key] < floor - DROP_TOLERANCE:
            failures.append(
                f"{key}: {cur[key]:.3f} < 基线 {floor:.3f} - 容差 {DROP_TOLERANCE}"
            )
    ok = not failures
    return ok, cur, failures


def write_baseline() -> dict:
    summary = _current_summary()
    payload = {
        "note": "local_eval 内置题集默认档(entity_channel=False, scene=aml)检索质量基线；"
                "任何检索侧改动(通道/融合/排序)后 hit@1/hit@5/mrr 与治理/维度比率下降 >0.02 即回归(A1, 2026-09-06)",
        "tolerance": DROP_TOLERANCE,
        "metrics": _flatten(summary),
        "summary_human": summary,
    }
    BASELINE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload["metrics"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Cortexa 检索质量回归门（A1）")
    ap.add_argument("--write-baseline", action="store_true",
                    help="重跑并把当前指标固化为基线")
    args = ap.parse_args()

    if args.write_baseline:
        metrics = write_baseline()
        print(f"[retrieval-gate] 基线已固化：{BASELINE_PATH}")
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 0

    ok, cur, failures = evaluate()
    print("[retrieval-gate] 当前档指标：")
    for k, v in sorted(cur.items()):
        print(f"  {k}: {v:.3f}" if isinstance(v, float) else f"  {k}: {v}")
    if ok:
        print("[retrieval-gate] PASS：检索分与治理/维度比率无回退")
        return 0
    for f in failures:
        print(f"[retrieval-gate] FAIL: {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
