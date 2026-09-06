"""LongMemEval-S -> aml_replay JSONL 转换器（A2/A3 本地回放素材，v0）。

上游：xiaowu0162/longmemeval 的 longmemeval_s（500 题，每题自带完整 haystack 会话，
278MB；HF 直连受限时经 HF_ENDPOINT=https://hf-mirror.com 下载）。

转换口径（本地代理，非官方；规模受控）：
- 每题 = 一个会话（conversation_id=question_id）。
- 先在整个 haystack 消息流中定位「内容包含 answer」的支撑轮(evidence)；
  再取该轮及其前的 max_messages 条消息作为回放语料——保证支撑记忆一定在库里
  （官方完整 haystack 形态留给 Smoke 回执后）。
- evidence_texts=[支撑轮原文]，gold=answer，meta 保留 question_type/date。
- 找不到支撑轮（如 answer 为空/答案不在消息原文）的题跳过并计数。

用法（单行）：
    python examples/convert_longmemeval_s.py --in data/aml-materials/longmemeval_s
        --out data/aml-materials/longmemeval_s_sample.jsonl --max-questions 16
        --seed 7 --max-messages 1200
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def pick_questions(data: list[dict], n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    idx = rng.sample(range(len(data)), min(n, len(data)))
    return [data[i] for i in sorted(idx)]


def flatten_turns(sessions_raw: list) -> list[tuple[str, str]]:
    """把嵌套会话消息流摊平为 (role, content) 列表（保序）。"""
    turns: list[tuple[str, str]] = []
    for chunk in sessions_raw:
        if isinstance(chunk, list):
            for m in chunk:
                if isinstance(m, dict) and m.get("content"):
                    turns.append((str(m.get("role", "user")), str(m["content"])))
        elif isinstance(chunk, dict) and chunk.get("content"):
            turns.append((str(chunk.get("role", "user")), str(chunk["content"])))
    return turns


def convert_item(q: dict, max_messages: int) -> dict | None:
    turns = flatten_turns(q.get("haystack_sessions") or [])
    if not turns:
        return None
    answer = str(q.get("answer") or "").strip()
    a_low = answer.lower()
    if not a_low:
        return None
    # 证据必须是"陈述型短轮"：gold 出现在内容里且该轮不长（长回答多为巧合命中，
    # 如 "$300" 偶现于吉他推荐——把巧合当支撑记忆会污染分桶，2026-09-06 实测）。
    # 先取最短命中轮（<=600 字符）；没有则整体放弃该题（宁可少不可错）。
    candidates = [
        i for i, (_r, c) in enumerate(turns) if a_low in c.lower()
    ]
    if not candidates:
        return None
    ev_idx = min(
        candidates,
        key=lambda i: len(turns[i][1]) if len(turns[i][1]) <= 600 else 10 ** 9,
    )
    if len(turns[ev_idx][1]) > 600:
        return None
    start = max(0, ev_idx - max_messages + 1)
    selected = turns[start: ev_idx + 1]
    return {
        "conversation_id": q["question_id"],
        "messages": [
            {"role": r, "content": c} for r, c in selected
        ],
        "questions": [{
            "qid": q["question_id"], "query": q.get("question") or "",
            "gold": answer,
            "evidence_texts": [selected[-1][1]],
            "meta": {"question_type": q.get("question_type"),
                     "question_date": q.get("question_date")},
        }],
        "meta": {"n_messages": len(selected), "evidence_idx": ev_idx},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="LongMemEval-S -> aml_replay JSONL")
    ap.add_argument("--in", dest="src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-questions", type=int, default=16)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--max-messages", type=int, default=1200)
    args = ap.parse_args()

    data = json.loads(args.src.read_text(encoding="utf-8"))
    picked = pick_questions(data, args.max_questions, args.seed)
    rows: list[dict] = []
    skipped = 0
    for q in picked:
        item = convert_item(q, args.max_messages)
        if item is None:
            skipped += 1
            continue
        rows.append(item)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    total_msgs = sum(r["meta"]["n_messages"] for r in rows)
    print(f"[convert] picked={len(picked)} kept={len(rows)} skip={skipped} "
          f"total_messages={total_msgs}")
    print(f"[convert] out={args.out}")


if __name__ == "__main__":
    main()
