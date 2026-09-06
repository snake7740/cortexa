# -*- coding: utf-8 -*-
"""文风病句扫描：端着腔（主谓宾不全/两字半句）定量检测 + 整改明细。

病灶定义（对齐用户口径：故意主谓宾不齐全的简略语句、两个字一个半句）：
  F2 碎片段   独立成段的无主语短语（"残魂。""筑基期。""终审。""三天。"）
  F3 独词句   叙述行内 <=2 字独立成句（"牙印。""渡口。""划。"）
  F5 短促链   叙述行内 >=3 个 <=2 字逗号子句连排（"认，收，平，划，…"）
  F1 辅助统计 短促子句占比（仅报告，不进明细）
豁免：对话行/行内夹对话、章标题、显式主语开头的完整句。

产出：-o 报告（章节排名）+ 同目录 *_detail.md（逐条明细含上下文，供整改执行）。
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

DIALOG_MARKS = '"\u201c\u201d\u300c\u300d'  # 直引号+弯引号，新旧卷兼容
SUBJECT_HEAD = (
    "叶长生", "苏晚晴", "秦洛", "晴娘", "渡舟", "周元白", "韩执事", "沈渊",
    "他", "她", "它", "我", "你", "您", "两人", "三人", "四人", "对方",
    "那人", "灰袍人", "老者", "弟子", "执事", "供奉", "父亲", "母亲",
    "周铁", "温客", "常生",
)
VERB_TAIL = r"(了|过|着|起来|下去|出来|回来|上去|不住|没有|不是|不能|不行|不对|得很|极了)$"


def strip_line(line: str) -> str:
    return line.strip().lstrip("#").strip()


def is_dialogue(line: str) -> bool:
    return line.startswith(('\"', '\u201c', '\u300c'))


def is_title(line: str) -> bool:
    return bool(re.match(r"^第\d+章", line))


def neighbor(lines: list[str], idx: int, step: int) -> str:
    """跨空行取最近的非空邻行（供整改定位上下文）。"""
    i = idx + step
    while 0 <= i < len(lines):
        s = strip_line(lines[i])
        if s:
            return s
        i += step
    return ""


def frag_kind(line: str) -> str | None:
    """独立段是否碎片。显式主语开头/多句段/超长段豁免。"""
    plain = re.sub(r"[，。！？…；、：,\.\?!;:]", "", line)
    if not plain or len(plain) > 8:
        return None
    if any(m in line for m in DIALOG_MARKS):
        return None
    if any(line.startswith(h) for h in SUBJECT_HEAD):
        return None
    # 段内含句末标点（去掉行尾那个）→ 多句段，非碎片
    if re.search(r"[。！？…]", line.rstrip("。！？…")):
        return None
    if "、" in line:
        segs = [x for x in plain and re.split(r"，|、", plain) if x]
        if len(segs) >= 2 and all(len(x) <= 3 for x in segs):
            return "ENUM"
    if re.search(VERB_TAIL, plain):
        return "BARE"
    return "NAME"


def scan_chapter(text: str) -> dict:
    f1_small, f1_total = 0, 0
    frags: list[tuple[int, str, str, str, str]] = []
    f3_items: list[tuple[int, str, str, str]] = []   # (行号, 前文, 独词句, 后文)
    f5_items: list[tuple[int, str]] = []             # (行号, 短促链行)
    f3, f5 = 0, 0
    lines = text.splitlines()
    for idx, raw in enumerate(lines):
        line = strip_line(raw)
        if not line or is_dialogue(line) or is_title(line):
            continue
        has_quote = any(m in line for m in DIALOG_MARKS)
        kind = frag_kind(line)
        if kind:
            frags.append(
                (idx + 1, kind, neighbor(lines, idx, -1)[:50], line[:50], neighbor(lines, idx, 1)[:50])
            )
        if has_quote:
            continue
        # 句内检测
        for body in re.split(r"[。！？…]+", line):
            body = body.strip()
            if not body:
                continue
            body_plain = re.sub(r"[，；、：,;:]", "", body)
            if 0 < len(body_plain) <= 2 and not any(body.startswith(h) for h in SUBJECT_HEAD):
                f3 += 1
                f3_items.append((idx + 1, neighbor(lines, idx, -1)[:40], body + "。", line[:50]))
            # F1 统计
            parts = [p for p in re.split(r"[，,；;]", body) if p.strip()]
            for p in parts:
                pp = re.sub(r"[、：:]", "", p.strip())
                if not pp:
                    continue
                f1_total += 1
                if len(pp) <= 3:
                    f1_small += 1
        # F5 短促链：>=3 个 <=2 字逗号子句连排
        clauses = [c.strip() for c in re.split(r"[，,]", line) if c.strip()]
        shorts = [c for c in clauses if len(re.sub(r"[、：:]", "", c)) <= 2]
        if len(shorts) >= 3 and len(shorts) >= len(clauses) // 2:
            f5 += 1
            f5_items.append((idx + 1, line[:60]))

    ratio = f1_small / f1_total if f1_total else 0.0
    score = round(ratio * 100, 1) + len(frags) * 2 + f3 * 3 + f5 * 4
    return {
        "f1_ratio": round(ratio * 100, 1),
        "frags": frags,
        "f3": f3,
        "f3_items": f3_items,
        "f5": f5,
        "f5_items": f5_items,
        "score": score,
        "total": len(frags) + f3 + f5,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("book")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--top", type=int, default=220)
    a = ap.parse_args()

    rows = []
    for f in sorted(Path(a.book).glob("Ch*.md")):
        m = re.match(r"Ch(\d+)_", f.name)
        if not m:
            continue
        rows.append((int(m.group(1)), scan_chapter(f.read_text(encoding="utf-8"))))

    rows.sort(key=lambda x: x[1]["score"], reverse=True)
    n = len(rows)
    hit = [(ch, r) for ch, r in rows if r["total"] > 0]

    lines = [
        "# 文风病句扫描报告（端着腔：主谓宾不全/两字半句）",
        "",
        f"- 扫描章节：{n} 章",
        f"- 有病灶章节：{len(hit)} 章；病灶总数：{sum(r['total'] for _, r in hit)} 处",
        "",
        "| 章 | 综合分 | 碎片段 | 独词句 | 短促链 | 短促子句% |",
        "|---|---|---|---|---|---|",
    ]
    for ch, r in rows[: a.top]:
        lines.append(
            f"| {ch} | {r['score']} | {len(r['frags'])} | {r['f3']} | {r['f5']} | {r['f1_ratio']}% |"
        )
    Path(a.out).write_text("\n".join(lines) + "\n", encoding="utf-8")

    detail = ["# 文风病灶明细（按章号升序，供整改执行）", ""]
    for ch, r in sorted(hit, key=lambda x: x[0]):
        detail.append(f"## Ch{ch}（共 {r['total']} 处）")
        for ln, kind, prev, cur, nxt in r["frags"]:
            detail.append(f"- L{ln} [碎片·{kind}] 前：{prev} ｜ 碎：{cur} ｜ 后：{nxt}")
        for ln, prev, cur, full in r["f3_items"]:
            detail.append(f"- L{ln} [独词句] 前：{prev} ｜ 词：{cur} ｜ 行：{full}")
        for ln, full in r["f5_items"]:
            detail.append(f"- L{ln} [短促链] 行：{full}")
        detail.append("")
    det_out = Path(a.out).with_name("style_scan_detail.md")
    det_out.write_text("\n".join(detail) + "\n", encoding="utf-8")
    print(f"报告 {a.out}（{n} 章，{len(hit)} 章有病灶，{sum(r['total'] for _, r in hit)} 处）")
    print(f"明细 {det_out}")
    print("\n".join(lines[:12]))


if __name__ == "__main__":
    main()
