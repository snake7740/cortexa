"""Q8 packaging proxy matrix（2026-09-06）。

固定 membership/order（模块不变量已测），只测包装档对『送答文本量』与召回侧
代理指标的影响。语料：LME-S 21 题样本 + InMind 125 会话。无 judge：本矩阵只
产出成本/覆盖代理数据，不给"更好"结论——包装能否提分留给判答层裁决。

用法：python examples/packaging_matrix.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import aml_replay  # noqa: E402

PKGS = ("raw", "pm1", "pm2", "raw-atomic")
CORPORA = {
    "lme21": ROOT / "data" / "aml-materials" / "longmemeval_s_sample.jsonl",
    "inmind": ROOT / "data" / "aml-materials" / "inmind_conv.jsonl",
}


def main() -> None:
    rows: list[dict] = []
    for cname, path in CORPORA.items():
        convs = aml_replay.load_convs(path)
        for pkg in PKGS:
            rep = aml_replay.run_replay_convs(convs, out_dir=None, top_k=20,
                                              packaging=pkg)
            t = rep["totals"]
            rows.append({
                "corpus": cname, "pkg": pkg,
                "buckets_hit": t["buckets"].get("hit", 0),
                "hit1": t["evidence_hit@1"], "hit5": t["evidence_hit@5"],
                "avg_tok_base": t["avg_tokens_per_query_base"],
                "avg_tok_full": t["avg_tokens_per_query_full"],
                "avg_tok_pkg": t["avg_tokens_per_query_packaged"],
                "pkg_chars": t["packaging_chars"],
                "q": t["questions"],
            })
            print(f"{cname} {pkg}: hit@{t['evidence_hit@1']}/{t['evidence_hit@5']} "
                  f"tok/query base={t['avg_tokens_per_query_base']} "
                  f"full={t['avg_tokens_per_query_full']} pkg={t['avg_tokens_per_query_packaged']} "
                  f"chars={t['packaging_chars']} buckets={t['buckets']}", flush=True)
    (ROOT / "cortexa-out" / "packaging_matrix.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
