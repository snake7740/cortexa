"""temporal_conflict_eval_v1 语料生成器（2026-09-06）。

裁决尺：F2 日历锚定 / R4 年份过滤 / RAR v2(重排) / RAR C4(reorder+keep-predecessor)
成对的固定测试集。本版=adversarial synthetic（验证算法不变量，不冒充 benchmark）；
L1 真实自然语言时间题待 LME-S 全量筛选（v1.1）。

JSONL 每行一会话：conversation_id / messages[{role,content,timestamp_ms}] /
questions[{qid,bucket(T1..T6),query,evidence_texts,gold,note}] /
statements[{content,subject,attr}] ← truth 表（false_conflict 归因）。

语义约定：
- 消息 timestamp_ms = 事件成立时刻（消息陈述即事件时刻）——F2/R4 都按此消费。
- 状态-区间(valid_from/valid_to)未建模：T2 用『事件年』问法，不用『某年时状态』。
- 整套 governance 关（全保留，冲突检索期裁决=AMI 路线）；治理开启是正交另一问。
- 真冲突 truth：同一 (subject,attr) 多版本；否则 false-conflict。
确定性（无随机）。用法：
  python examples/gen_temporal_eval.py   # -> examples/eval_data/temporal_conflict_eval_v1.jsonl
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "eval_data" / "temporal_conflict_eval_v1.jsonl"


def ts(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


def S(content, at, subject, attr) -> dict:
    return {"content": content, "ts": at, "subject": subject, "attr": attr}


def _conv(cid: str, stmts: list[dict], questions: list[dict]) -> dict:
    texts = {s["content"] for s in stmts}
    for q in questions:
        assert all(e in texts for e in q["evidence_texts"]), (cid, q["qid"])
    assert len(texts) == len(stmts), f"{cid} 重复 content"
    return {
        "conversation_id": cid,
        "messages": [{"role": "user", "content": s["content"], "timestamp_ms": s["ts"]}
                     for s in stmts],
        "questions": questions,
        "statements": [{"content": s["content"], "subject": s["subject"], "attr": s["attr"]}
                       for s in stmts],
    }


# ---------- T1/T2/T4 住所版本史 ----------
RES = [  # (old_year, old_month, cityA, new_year, new_month, cityB)
    (2025, 3, "Seattle", 2026, 2, "Boston"),
    (2024, 8, "Denver", 2025, 9, "Portland"),
    (2024, 12, "Chicago", 2026, 1, "Austin"),
    (2023, 6, "Nashville", 2025, 5, "Tampa"),
    (2022, 11, "Buffalo", 2024, 4, "Phoenix"),
    (2025, 1, "Memphis", 2026, 3, "Madison"),
    (2024, 2, "Reno", 2025, 7, "Tulsa"),
    (2023, 9, "Fargo", 2024, 10, "Dover"),
    (2025, 4, "Albany", 2026, 4, "Boise"),
    (2021, 12, "Eugene", 2023, 3, "Akron"),
]


def scen_residence() -> list[dict]:
    out = []
    for i, (oy, om, ca, ny, nm, cb) in enumerate(RES):
        old = S(f"Alice moved to {ca} in {oy}.", ts(oy, om, 15), "alice", "residence")
        new = S(f"Alice moved to {cb} in {ny}.", ts(ny, nm, 10), "alice", "residence")
        out.append(_conv(f"res-{i}", [old, new], [
            {"qid": f"res-{i}-t1", "bucket": "T1",
             "query": "Where does Alice currently live?",
             "evidence_texts": [new["content"]], "gold": cb,
             "note": "current-state"},
            {"qid": f"res-{i}-t2", "bucket": "T2",
             "query": f"Which city did Alice move to in {oy}?",
             "evidence_texts": [old["content"]], "gold": ca,
             "note": f"event-year={oy}"},
            {"qid": f"res-{i}-t4", "bucket": "T4",
             "query": f"Did Alice live in {ca} before or after {cb}?",
             "evidence_texts": [old["content"]], "gold": "before",
             "note": "顺序（before/after 措辞）"},
        ]))
    return out


# ---------- 雇主版本史 + 属格 decoy（T1/T2/T6） ----------
EMP = [  # (companyA, yearA, decoy_company, decoy_year, companyB, yearB)
    ("Google", 2023, "Microsoft", 2024, "Acme Corp", 2025),
    ("IBM", 2022, "Oracle", 2023, "Nimbus Labs", 2025),
    ("Tesla", 2024, "Rivian", 2025, "Volt Motors", 2026),
    ("Stripe", 2023, "Square", 2024, "Payflow Inc", 2026),
    ("Novartis", 2021, "Pfizer", 2022, "Medora Bio", 2025),
    ("Deloitte", 2020, "EY", 2021, "KPMG", 2024),
]


def scen_employer() -> list[dict]:
    out = []
    for i, (ca, ya, dc, dy, cb, yb) in enumerate(EMP):
        rel = "sister" if i % 2 else "brother"
        st1 = S(f"Alice joined {ca} in {ya}.", ts(ya, 6, 1), "alice", "employer")
        dec = S(f"Alice's {rel} joined {dc} in {dy}.", ts(dy, 9, 15), "alice", "employer-other")
        st2 = S(f"Alice now works at {cb}.", ts(yb, 11, 20), "alice", "employer")
        out.append(_conv(f"emp-{i}", [st1, dec, st2], [
            {"qid": f"emp-{i}-t1", "bucket": "T1",
             "query": "Where does Alice work now?",
             "evidence_texts": [st2["content"]], "gold": cb,
             "note": "最新雇主排前"},
            {"qid": f"emp-{i}-t2", "bucket": "T2",
             "query": f"Which company did Alice join in {ya}?",
             "evidence_texts": [st1["content"]], "gold": ca,
             "note": f"event-year={ya}"},
            {"qid": f"emp-{i}-t6", "bucket": "T6",
             "query": f"Which company does Alice's {rel} work at?",
             "evidence_texts": [dec["content"]], "gold": dc,
             "note": "False-Conflict：属格转移句不得与本人雇主句成冲突簇"},
        ]))
    # 三版本咨询链（C4 压缩 + 历史列表题）
    for i, (k1, k2, k3) in enumerate([("Kay", "Lou", "Mia"), ("Rex", "Sam", "Tia"),
                                      ("Uma", "Val", "Wes"), ("Ned", "Ora", "Pia")]):
        a = S(f"Team Nova hired consultant {k1} in 2023.", ts(2023, 3, 1), "team nova", "consultant")
        b = S(f"Team Nova switched to consultant {k2} in 2024.", ts(2024, 4, 1), "team nova", "consultant")
        c = S(f"Team Nova currently uses consultant {k3}.", ts(2026, 1, 10), "team nova", "consultant")
        out.append(_conv(f"con-{i}", [a, b, c], [
            {"qid": f"con-{i}-t1", "bucket": "T1",
             "query": "Which consultant does Team Nova currently use?",
             "evidence_texts": [c["content"]], "gold": k3, "note": "三版本史"},
            {"qid": f"con-{i}-t5", "bucket": "T5",
             "query": "Which consultants has Team Nova used over time?",
             "evidence_texts": [a["content"], b["content"], c["content"]],
             "gold": f"{k1}, {k2}, {k3}", "note": "历史列表——C4 压缩不得伤此"},

        ]))
    return out


# ---------- T3 日历窗（含边界） ----------
CAL = [
    # (now_ymd, 干扰事件, 目标事件, 问题, 期望)
    dict(now=(2026, 4, 10), y=(2026, 2, 25), yc="Alice met professor Kim in late February.",
         x=(2026, 3, 10), xc="Alice met her cousin in Nice on March 10.",
         q="Who did Alice meet last month?", gold="cousin"),
    dict(now=(2026, 3, 31), y=(2026, 3, 30), yc="Alice watched a film on March 30.",
         x=(2026, 2, 28), xc="Alice went to the opera on February 28.",
         q="What did Alice do last month?", gold="opera"),
    dict(now=(2026, 1, 5), y=(2025, 11, 3), yc="Alice bought a tent in November.",
         x=(2025, 1, 20), xc="Alice bought a kayak in January 2025.",
         q="What did Alice buy last year?", gold="kayak, tent"),
    dict(now=(2024, 4, 30), y=(2024, 3, 15), yc="Alice hiked Table Rock in March.",
         x=(2024, 2, 29), xc="Alice started yoga on February 29.",
         q="What did Alice do last year?", gold="yoga"),
    dict(now=(2026, 4, 10), y=(2026, 2, 14), yc="Alice baked cookies in mid February.",
         x=(2026, 1, 31), xc="Alice submitted the report on January 31.",
         q="What did Alice do 3 months ago?", gold="report"),
    dict(now=(2026, 6, 30), y=(2026, 5, 28), yc="Alice gardened in late May.",
         x=(2026, 4, 30), xc="Alice hosted a dinner party on April 30.",
         q="What did Alice do last month?", gold="dinner"),
    dict(now=(2025, 12, 31), y=(2025, 11, 30), yc="Alice jogged in November.",
         x=(2025, 10, 31), xc="Alice painted the fence on Halloween.",
         q="What did Alice do two months ago?", gold="fence"),
    dict(now=(2024, 3, 1), y=(2024, 2, 10), yc="Alice skied in early February.",
         x=(2023, 12, 28), xc="Alice visited the planetarium in December.",
         q="What did Alice do last year?", gold="planetarium"),
    dict(now=(2026, 5, 1), y=(2026, 4, 2), yc="Alice cleaned the garage in April.",
         x=(2026, 3, 31), xc="Alice booked the flight on March 31.",
         q="What did Alice do last month?", gold="flight"),
    dict(now=(2023, 2, 28), y=(2023, 1, 25), yc="Alice ran a 10k in January.",
         x=(2022, 12, 31), xc="Alice threw a party on New Year's Eve.",
         q="What did Alice do last year?", gold="party"),
    dict(now=(2026, 4, 15), y=(2026, 3, 28), yc="Alice tried salsa dancing in March.",
         x=(2026, 2, 27), xc="Alice tried pottery in late February.",
         q="What hobby did Alice try last month?", gold="salsa"),
    dict(now=(2026, 2, 28), y=(2026, 1, 31), yc="Alice updated her website in January.",
         x=(2025, 12, 25), xc="Alice launched the newsletter in December.",
         q="What did Alice do last year?", gold="newsletter"),
]


def scen_calendar() -> list[dict]:
    out = []
    for i, c in enumerate(CAL):
        ny, nm, nd = c["now"]
        y_m = S(c["yc"], ts(*c["y"]), "alice", "activity")
        x_m = S(c["xc"], ts(*c["x"]), "alice", "activity")
        # now 锚：加一条当前时刻的无关消息让 scope max ts=now（会话语境）
        now_m = S("Alice is planning her summer trip.", ts(ny, nm, nd), "alice", "plan")
        out.append(_conv(f"cal-{i}", [x_m, y_m, now_m], [
            {"qid": f"cal-{i}-t3", "bucket": "T3", "query": c["q"],
             "evidence_texts": [x_m["content"]], "gold": c["gold"],
             "note": f"now={c['now'][0]}-{c['now'][1]:02d}-{c['now'][2]:02d} 日历窗 vs 30 天近似边界"},
        ]))
    return out


# ---------- T5 列表题（供应商/项目/联系人 版本史） ----------
def scen_projects() -> list[dict]:
    out = []
    letters = [("A", "B", "C", "D"), ("E", "F", "G", "H"), ("K", "L", "M", "N")]
    for i, (w, x, y, z) in enumerate(letters):
        a = S(f"Team Nova worked with vendors {w} and {x} in 2024.", ts(2024, 5, 12), "team nova", "vendor-list")
        b = S(f"Team Nova worked with vendors {x} and {y} in 2025.", ts(2025, 5, 12), "team nova", "vendor-list")
        c = S(f"Team Nova currently works with vendors {y} and {z}.", ts(2026, 4, 1), "team nova", "vendor-list")
        out.append(_conv(f"proj-{i}", [a, b, c], [
            {"qid": f"proj-{i}-t5", "bucket": "T5",
             "query": "Which vendors does Team Nova currently work with?",
             "evidence_texts": [c["content"]], "gold": f"{y} and {z}",
             "note": "三版本列表——C4 应只返最新(no-extras 面)"},
            {"qid": f"proj-{i}-t4", "bucket": "T4",
             "query": "Which vendor pair did Team Nova use right before the current one?",
             "evidence_texts": [b["content"]], "gold": f"{x} and {y}",
             "note": "predecessor 定位"},
        ]))
    # 真实项目列表（同 subject 并列演化 + 已结束项）
    for i, (p1n, p2n) in enumerate([("Atlas", "Beacon"), ("Cedar", "Dune"), ("Elm", "Fern")]):
        p1 = S(f"Bob is working on the {p1n} project since 2025.", ts(2025, 6, 1), "bob", "project-list")
        p2 = S(f"Bob is working on the {p2n} project since 2026.", ts(2026, 2, 1), "bob", "project-list")
        done = S(f"Bob finished the {p1n}-legacy project in 2024.", ts(2024, 12, 10), "bob", "project-done")
        out.append(_conv(f"blist-{i}", [p1, p2, done], [
            {"qid": f"blist-{i}-t5", "bucket": "T5",
             "query": "Which projects is Bob currently working on?",
             "evidence_texts": [p1["content"], p2["content"]], "gold": f"{p1n}, {p2n}",
             "note": "列表两版并存（非单值冲突）+ done 项不得混入当前"},
            {"qid": f"blist-{i}-t6", "bucket": "T6",
             "query": "Which project did Bob finish in 2024?",
             "evidence_texts": [done["content"]], "gold": f"{p1n}-legacy",
             "note": "已结束≠当前冲突"},
        ]))
    return out


# ---------- 属性/多值 + 跨属性并存（T1/T6） ----------
def scen_attrs() -> list[dict]:
    out = []
    pairs = [  # (attr, 值A, 值B, 名)
        ("pet", "a dog named Rex", "a cat named Luna", "Alice", "alice"),
        ("diet", "vegetarian", "vegan", "Alice", "alice"),
        ("car", "a gray sedan", "a red hatchback", "Bob", "bob"),
        ("phone", "an iPhone", "a Pixel", "Bob", "bob"),
        ("coffee", "flat white", "cold brew", "Dana", "dana"),
        ("city", "living in Oslo", "living in Bergen", "Dana", "dana"),
    ]
    for i, (attr, va, vb, display, subj) in enumerate(pairs):
        if attr == "city":
            a = S(f"{display} prefers {va} since 2024.", ts(2024, 5, 1), subj, "city-pref")
            b = S(f"{display} prefers {vb} since 2026.", ts(2026, 1, 1), subj, "city-pref")
            q = f"Which city does {display} prefer now?"
        else:
            a = S(f"{display}'s {attr} is {va}.", ts(2024, 5, 1), subj, attr)
            b = S(f"{display}'s {attr} is now {vb}.", ts(2026, 1, 1), subj, attr)
            q = f"What {attr} does {display} have now?"
        out.append(_conv(f"attr-{i}", [a, b], [
            {"qid": f"attr-{i}-t1", "bucket": "T1", "query": q,
             "evidence_texts": [b["content"]], "gold": vb, "note": f"{attr} 版本史"},
        ]))
    # Bob 居住+工作 双簇并存（跨属性不相干）
    b1 = S("Bob lives in Austin since 2025.", ts(2025, 4, 5), "bob", "residence")
    b2 = S("Bob lives in Dallas since 2026.", ts(2026, 1, 20), "bob", "residence")
    w1 = S("Bob works at the bank in 2024.", ts(2024, 7, 1), "bob", "employer")
    w2 = S("Bob works at the studio since 2026.", ts(2026, 2, 1), "bob", "employer")
    out.append(_conv("attr-bob", [b1, b2, w1, w2], [
        {"qid": "attr-bob-t1a", "bucket": "T1", "query": "Where does Bob currently live?",
         "evidence_texts": [b2["content"]], "gold": "Dallas", "note": "residence 簇"},
        {"qid": "attr-bob-t1b", "bucket": "T1", "query": "Where does Bob currently work?",
         "evidence_texts": [w2["content"]], "gold": "studio", "note": "employer 簇"},
        {"qid": "attr-bob-t6", "bucket": "T6", "query": "Where did Bob work in 2024?",
         "evidence_texts": [w1["content"]], "gold": "bank", "note": "跨属性共存不互吞"},
    ]))
    return out


def build() -> list[dict]:
    convs = (scen_residence() + scen_employer() + scen_calendar()
             + scen_projects() + scen_attrs())
    qids, counts = [], Counter()
    for cv in convs:
        for q in cv["questions"]:
            qids.append(q["qid"])
            counts[q["bucket"]] += 1
        texts = {s["content"] for s in cv["statements"]}
        for q in cv["questions"]:
            assert all(e in texts for e in q["evidence_texts"]), (cv["conversation_id"], q["qid"])
    assert len(qids) == len(set(qids)), "qid 必须唯一"
    return convs, counts


def main() -> None:
    convs, counts = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for cv in convs:
            f.write(json.dumps(cv, ensure_ascii=False) + "\n")
    total = sum(counts.values())
    print(f"[gen] convs={len(convs)} questions={total}")
    print("buckets:", dict(sorted(counts.items())))


if __name__ == "__main__":
    main()
