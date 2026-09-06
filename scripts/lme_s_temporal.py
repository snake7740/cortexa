"""LME-S 全量时间题提取（迁移验证第一层，2026-09-06）。

阶段 1：确定性规则全量扫描 longmemeval_s(500 题/278MB) 的 question 文本，
筛出时间/意图相关候选 → 输出 candidates_preview 供第二层人工按题意打
gold_intent（CURRENT/HISTORICAL/RELATIVE/SEQUENCE/ENUMERATION/
CURRENT_ENUMERATION/OTHER_TEMPORAL）；不参考任何配置结果。
阶段 2（后续）：标注后转换 manifest（messages+evidence+合成相对 ts，冻结）。

ts 说明：上游 LME-S 无消息时间戳——合成『会话内相对序』时间戳（question_date
锚点若可用否则 2026-01-01，消息按序向 past 铺开）。日历 F2/R4 在此不可证
（无真实日期），本迁移只验证 RAR(相对 recency)+意图门在自然语言分布上的泛化。

用法：python scripts/lme_s_temporal.py --in data/aml-materials/longmemeval_s
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path

_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_WORD = re.compile(r"[a-z0-9']+")

CUR = {"current", "currently", "now", "latest", "present", "recent", "recently", "today", "today's", "nowadays"}
SEQ = {"before", "after", "earlier", "later", "first", "earliest", "next", "followed"}
REL = re.compile(r"(last\s+(year|month|week|few|time)|ago|since|previously|used\s+to|in\s+(19|20)\d{2}|last\s+time)")
ENUM = {"all", "list", "which", "every", "each", "any", "who", "names"}
OTHER = {"when", "whenever", "how\s+many", "how\s+often", "how\s+long", "at\s+that\s+time", "back\s+then", "then"}


def classify_layer1(q: str) -> list[str]:
    low = q.lower()
    toks = set(_WORD.findall(low))
    tags: list[str] = []
    if toks & CUR:
        tags.append("CUR")
    if toks & SEQ:
        tags.append("SEQ")
    if _YEAR.search(q) or REL.search(low):
        tags.append("REL")
    if toks & ENUM:
        tags.append("ENUM")
    if any(o in low for o in OTHER):
        tags.append("OTHER")
    return tags or ["NONE"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", type=Path, required=True)
    ap.add_argument("--out", dest="out", type=Path,
                    default=Path("data/aml-materials/lme_s_temporal_candidates.jsonl"))
    ap.add_argument("--cap", type=int, default=220, help="候选上限（防标注爆炸）")
    args = ap.parse_args()
    hits: list[dict] = []
    tag_counter: Counter = Counter()
    t0 = time.time()
    # 上游为多行 pretty JSON 数组（278MB）；整载后逐题过滤（此前样例转换已证可行）
    data = json.loads(args.src.read_text(encoding="utf-8"))
    for q in data:
        question = q.get("question") or ""
        tags = classify_layer1(question)
        if tags != ["NONE"]:
            tag_counter["|".join(sorted(tags))] += 1
            hits.append({"question_id": q["question_id"], "question": question,
                         "question_type": q.get("question_type"),
                         "question_date": q.get("question_date"),
                         "tags": tags})
        if len(hits) >= args.cap:
            break
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as f:
            for h in hits:
                f.write(json.dumps(h, ensure_ascii=False) + "\n")
    print(f"[scan] records={len(data)} in {time.time()-t0:.0f}s; hits={len(hits)}")
    print("tag groups:", dict(tag_counter.most_common(20)))


if __name__ == "__main__":
    main()
