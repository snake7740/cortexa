"""InMind (imlrz/InMind, 125 隐式关联任务) -> aml_replay JSONL 转换器。

目的（T2.2，2026-09-06）：路由缺口测量——direct(naive_query) 与 indirect(query)
对决定性个人事实(user_message)的检索召回是否一致；InMind 论文控的『Target recall』
在此=间接问题下决定性事实是否进入 top_k。

每记录→一个会话(messages=user_message+assistant_message)，两道题：
  - <id>-direct  : naive_query，evidence=user_message
  - <id>-indirect: query，evidence=user_message（桥=relation/explanation 在 meta 存档）

用法：
    python examples/convert_inmind.py --in data/aml-materials/inmind.jsonl
        --out data/aml-materials/inmind_conv.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="InMind -> aml_replay JSONL")
    ap.add_argument("--in", dest="src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    rows: list[dict] = []
    for line in args.src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        um = rec["user_message"]
        qs = [
            {"qid": str(rec["task_id"]) + "-direct",
             "query": rec["naive_query"],
             "evidence_texts": [um],
             "gold": "",
             "meta": {"kind": "direct", "domain": rec.get("domain")}},
            {"qid": str(rec["task_id"]) + "-indirect",
             "query": rec["query"],
             "evidence_texts": [um],
             "gold": "",
             "meta": {"kind": "indirect", "domain": rec.get("domain"),
                      "relation": rec.get("relation", ""),
                      "explanation": rec.get("explanation", "")}},
        ]
        rows.append({
            "conversation_id": "inmind-" + str(rec["task_id"]),
            "messages": [
                {"role": "user", "content": um},
                {"role": "assistant", "content": rec["assistant_message"]},
            ],
            "questions": qs,
            "meta": {"n_messages": 2},
        })
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[inmind] rows={len(rows)} out={args.out}")


if __name__ == "__main__":
    main()
