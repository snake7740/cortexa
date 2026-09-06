"""LME-S 时间 manifest v1 构建（迁移验证第二层标注 + 冻结，2026-09-06）。

标注口径：按题意（question 文本 + question_type）打 gold_intent，不参考任何
C2/G3 结果。标签：CURRENT / CURRENT_ENUMERATION / HISTORICAL / RELATIVE /
SEQUENCE / ENUMERATION / OTHER_TEMPORAL；NON_TEMPORAL（偏好/建议/对话引用
检索类）剔除不进 manifest。规则即题意编码：顺序词 > 相对时间 > 状态历史 >
当前态 > 枚举 > 其余。产出冻结文件（此后所有算法改动复用同一 manifest）。

ts：上游无消息时间戳 → 合成会话内相对序（question_date 或 2026-01-01 锚点，
消息自后向前按 60s 间隔铺开）。日历件不可证，本迁移只验 RAR(相对序)+意图门泛化。

用法：python scripts/build_lme_s_manifest.py --cand data/aml-materials/lme_s_temporal_candidates.jsonl
      --src data/aml-materials/longmemeval_s --out data/aml-materials/lme_s_temporal_manifest_v1.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_YEAR = re.compile(r"\b(19|20)\d{2}\b")

NON = "NON_TEMPORAL"
SEQA = re.compile(r"\b(first|earliest|which[^.]*first|order[^.]*earliest|earliest to latest|first to last)\b")
SEQA2 = re.compile(r"(which (event|item|device|task|trip|book|group|seeds|pair|shoes|vehicle|airline|mode|gift|project|show|health issue|streaming service|company)[^.]*(first|earlier))", re.I)
REL = re.compile(
    r"\b(ago|yesterday|last (week|month|weekend|year|night|few|time|saturday|monday|tuesday|issue)|"
    r"past (month|week|two|few)|recent(ly)?|since\b|over the past|this weekend|"
    r"few (weeks|months|days)|couple of days|a (week|month|few) ago|"
    r"earlier\b|two weeks ago|months ago|days ago|weeks ago|\d+ (days?|weeks?|months?|years?) ago)\b",
    re.I,
)
HIST = re.compile(
    r"\b(when did|when was|what was|before\b|previous|how old was i when|at which|"
    r"back then|that time|previously|used to|then\b|did i (receive|have|go|attend|visit|watch|buy|meet|participate|start|finish|complete)|"
    r"the (first|last) time|how many days? (did|before|between|passed)|how long (did|had|have) i|how (many weeks|many months) had|"
    r"was it (before|after))\b",
    re.I,
)
CURW = re.compile(r"\b(current(ly)?|now\b|latest|most recent(ly)?|these days|today\b|nowadays)\b", re.I)
ENUMW = re.compile(r"\b(which|all|list|every|each|how many|names?)\b", re.I)
OTH = re.compile(r"\b(when\b|whenever|how often|how long have i been|how long have i been working|will i be|how many years will|interval)\b", re.I)


def label_question(q: str, qtype: str) -> str:
    if qtype in ("single-session-preference", "single-session-assistant"):
        return NON
    low = q.lower()
    if SEQA.search(low) or SEQA2.search(low):
        return "SEQUENCE"
    if REL.search(low):
        # 状态对（previous/current 同题）归历史态，其余相对时间
        if HIST.search(low) and ("previous" in low or "before" in low):
            return "HISTORICAL"
        return "RELATIVE"
    if HIST.search(low):
        return "HISTORICAL"
    if CURW.search(low):
        if ENUMW.search(low):
            return "CURRENT_ENUMERATION"
        return "CURRENT"
    if ENUMW.search(low):
        return "ENUMERATION"
    if OTH.search(low):
        return "OTHER_TEMPORAL"
    return NON


def anchor_ts(q_date: str | None) -> int:
    """question_date 若可解析为锚；否则 2026-01-01。UTC 正午。"""
    try:
        dt = datetime.fromisoformat((q_date or "").replace("Z", "+00:00"))
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return int(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


def flatten(turns_raw: list) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for chunk in turns_raw or []:
        if isinstance(chunk, list):
            for m in chunk:
                if isinstance(m, dict) and m.get("content"):
                    out.append((str(m.get("role", "user")), str(m["content"])))
        elif isinstance(chunk, dict) and chunk.get("content"):
            out.append((str(chunk.get("role", "user")), str(chunk["content"])))
    return out


def convert_row(q: dict, label: str, idx: int, max_messages: int = 1200) -> dict | None:
    turns = flatten(q.get("haystack_sessions") or [])
    if not turns:
        return None
    answer = str(q.get("answer") or "").strip()
    if not answer:
        return None
    a_low = answer.lower()
    cand = [i for i, (_r, c) in enumerate(turns) if a_low in c.lower()
            and len(c) <= 600]
    if not cand:
        return None
    ev_idx = min(cand, key=lambda i: len(turns[i][1]))
    start = max(0, ev_idx - max_messages + 1)
    sel = turns[start: ev_idx + 1]
    anchor = anchor_ts(q.get("question_date"))
    # 合成相对时间戳：消息自后向前 60s 间隔（会话内相对序保持）
    n = len(sel)
    msgs = []
    for k, (role, content) in enumerate(sel):
        msgs.append({"role": role, "content": content,
                     "timestamp_ms": int(anchor - (n - 1 - k) * 60_000)})
    return {
        "conversation_id": f"lme-{idx:04d}",
        "messages": msgs,
        "questions": [{
            "qid": f"lme-{idx:04d}", "query": q.get("question") or "",
            "gold": answer, "evidence_texts": [sel[-1][1]],
            "gold_intent": label,
            "meta": {"question_type": q.get("question_type"),
                     "question_date": q.get("question_date"),
                     "src_qid": q.get("question_id")},
        }],
        "meta": {"n_messages": len(msgs), "evidence_idx": ev_idx,
                 "src_qid": q.get("question_id"), "label": label},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cand", type=Path, required=True)
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    cands = [json.loads(_ln) for _ln in args.cand.read_text(encoding="utf-8").split("\n") if _ln.strip()]
    data = json.loads(args.src.read_text(encoding="utf-8"))
    by_src = {(d.get("question_id"), d.get("question")): d for d in data}
    labels: Counter = Counter()
    skipped_non = skipped_ev = 0
    rows: list[dict] = []
    idx = 0
    for c in cands:
        label = label_question(c["question"], c.get("question_type") or "")
        labels[label] += 1
        if label == NON:
            skipped_non += 1
            continue
        raw = by_src.get((c["question_id"], c["question"]))
        if raw is None:
            skipped_non += 1
            continue
        row = convert_row(raw, label, idx)
        idx += 1
        if row is None:
            skipped_ev += 1
            continue
        rows.append(row)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("labels:", dict(labels))
    print(f"manifest rows={len(rows)} skip_non={skipped_non} skip_ev={skipped_ev}")
    kept = Counter(r["questions"][0]["gold_intent"] for r in rows)
    print("kept by label:", dict(kept))


if __name__ == "__main__":
    main()
