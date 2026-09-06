# -*- coding: utf-8 -*-
"""文风画像记忆维度：每章文笔风格扫描 -> 画像入库 -> 提问式索引直接命中。

解耦定位（三主体契约，与 novel_bridge 同款）：
- 文笔检测规则属于创作系统域（本脚本，应用侧），Cortexa 核心零改动；
- 画像作为独立记忆维度落 BOOK_ROOT/.cortexa（scope=<书名>-style），
  条目用【第N章·文风】锚定前缀——复用核心已有的编号精确通道，
  查询"第60章的文风"直接 LIKE 命中，不做全库遍历；
- 总览条目（【文风总览】）携带全部病灶章号清单，全局提问一次命中；
- sync 前先 forget_scope 全量刷新：整改后画像自动更新，无新旧叠加。

病灶口径（对齐用户定义：故意主谓宾不齐全的简略语句、两个字一个半句）：
  F2 碎片段   独立成段的无主语短语（"残魂。""筑基期。""三天。"）
  F3 独词句   叙述行内 <=2 字独立成句（"牙印。""渡口。""划。"）
  F5 短促链   叙述行内 >=3 个 <=2 字逗号子句连排（"认，收，平，划，…"）
豁免：对话行/行内夹对话、章标题、显式主语开头的完整句。

用法：
    python examples/novel_style.py <BOOK_ROOT> scan            # 只扫+落盘 JSON/报告
    python examples/novel_style.py <BOOK_ROOT> sync            # 扫描+画像入库（幂等刷新）
    python examples/novel_style.py <BOOK_ROOT> query "哪些章还有端着腔"
    python examples/novel_style.py <BOOK_ROOT> query "第60章的文风" --top-k 3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# 画像库刻意走本地 hash 嵌入（零 API；检索主力=FTS bigram+编号通道）
os.environ.pop("OPENAI_API_KEY", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 自足：不依赖 cwd/PYTHONPATH

from cortexa import Cortexa
from cortexa.modules.consistency import LOGICAL_T0

MAX_DETAIL_IN_MEMORY = 20  # 单章画像条目内嵌明细上限（全量在 style_profile.json）
FRAG_LABEL = {"NAME": "碎片·名词", "BARE": "碎片·裸谓", "ENUM": "碎片·枚举"}

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


def frag_kind(line: str) -> str | None:
    """独立段是否碎片。显式主语开头/多句段/超长段豁免。"""
    plain = re.sub(r"[，。！？…；、：,\.\?!;:]", "", line)
    if not plain or len(plain) > 8:
        return None
    if any(m in line for m in DIALOG_MARKS):
        return None
    if any(line.startswith(h) for h in SUBJECT_HEAD):
        return None
    if re.search(r"[。！？…]", line.rstrip("。！？…")):
        return None
    if "、" in line:
        segs = [x for x in re.split(r"，|、", plain) if x]
        if len(segs) >= 2 and all(len(x) <= 3 for x in segs):
            return "ENUM"
    if re.search(VERB_TAIL, plain):
        return "BARE"
    return "NAME"


def scan_chapter(text: str) -> dict:
    """单章扫描：返回 {total, frags, f3, f5, score, items}（items=结构化明细）。"""
    f1_small, f1_total = 0, 0
    items: list[dict] = []
    f3 = f5 = 0
    lines = text.splitlines()
    for idx, raw in enumerate(lines):
        line = strip_line(raw)
        if not line or is_dialogue(line) or is_title(line):
            continue
        kind = frag_kind(line)
        if kind:
            items.append({"line": idx + 1, "type": f"碎片段({FRAG_LABEL[kind]})", "text": line[:60]})
        if any(m in line for m in DIALOG_MARKS):
            continue
        for body in re.split(r"[。！？…]+", line):
            body = body.strip()
            if not body:
                continue
            body_plain = re.sub(r"[，；、：,;:]", "", body)
            if 0 < len(body_plain) <= 2 and not any(body.startswith(h) for h in SUBJECT_HEAD):
                f3 += 1
                items.append({"line": idx + 1, "type": "独词句", "text": f"{body}。｜行:{line[:40]}"})
            parts = [p for p in re.split(r"[，,；;]", body) if p.strip()]
            for p in parts:
                pp = re.sub(r"[、：:]", "", p.strip())
                if not pp:
                    continue
                f1_total += 1
                if len(pp) <= 3:
                    f1_small += 1
        clauses = [c.strip() for c in re.split(r"[，,]", line) if c.strip()]
        shorts = [c for c in clauses if len(re.sub(r"[、：:]", "", c)) <= 2]
        if len(shorts) >= 3 and len(shorts) >= len(clauses) // 2:
            f5 += 1
            items.append({"line": idx + 1, "type": "短促链", "text": line[:60]})

    frags = sum(1 for i in items if i["type"].startswith("碎片段"))
    ratio = f1_small / f1_total if f1_total else 0.0
    score = round(ratio * 100, 1) + frags * 2 + f3 * 3 + f5 * 4
    return {"total": len(items), "frags": frags, "f3": f3, "f5": f5,
            "f1_ratio": round(ratio * 100, 1), "score": score, "items": items}


def _chapter_dir(root: Path) -> Path | None:
    for cand in ("废灵根", "正文", "chapters"):
        d = root / cand
        if d.exists() and list(d.glob("Ch*_*.md")):
            return d
    return None


def scan_book(root: Path) -> dict:
    """全库扫描 -> {chapters: {ch: scan_result}, titles: {ch: title}}（章号升序）。"""
    ch_dir = _chapter_dir(root)
    if ch_dir is None:
        raise SystemExit(f"未找到章节目录（废灵根/正文/chapters）：{root}")
    chapters: dict[int, dict] = {}
    for md in sorted(ch_dir.glob("Ch*_*.md")):
        m = re.match(r"Ch(\d+)_(.+)\.md", md.name)
        if not m:
            continue
        ch = int(m.group(1))
        chapters[ch] = scan_chapter(md.read_text(encoding="utf-8"))
    return {"chapters": chapters}


def _out_dir(root: Path) -> Path:
    d = root / "cortexa-out"
    d.mkdir(exist_ok=True)
    return d


def cmd_scan(root: Path) -> int:
    profile = scan_book(root)
    chapters = profile["chapters"]
    hit = {ch: r for ch, r in chapters.items() if r["total"] > 0}
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "criteria": "端着腔=故意主谓宾不齐全的简略语句/两个字一个半句；"
                    "碎片段/独词句/短促链三类，对话与完整句豁免",
        "chapter_count": len(chapters),
        "hit_count": len(hit),
        "total_items": sum(r["total"] for r in hit.values()),
        "chapters": {str(ch): chapters[ch] for ch in sorted(chapters)},
    }
    p = _out_dir(root) / "style_profile.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [
        f"# 文风画像扫描报告（{payload['generated_at']}）",
        "",
        f"- 口径：{payload['criteria']}",
        f"- 扫描 {payload['chapter_count']} 章：有病灶 {payload['hit_count']} 章，"
        f"共 {payload['total_items']} 处；干净 {payload['chapter_count'] - payload['hit_count']} 章",
        f"- 病灶章：{'、'.join(str(c) for c in sorted(hit))}",
        "",
        "| 章 | 综合分 | 病灶数 | 碎片段 | 独词句 | 短促链 |",
        "|---|---|---|---|---|---|",
    ]
    for ch, r in sorted(hit.items(), key=lambda x: -x[1]["score"]):
        lines.append(f"| {ch} | {r['score']} | {r['total']} | {r['frags']} | {r['f3']} | {r['f5']} |")
    rp = _out_dir(root) / "style_scan_report.md"
    rp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[scan] {payload['chapter_count']} 章，病灶 {payload['hit_count']} 章/{payload['total_items']} 处")
    print(f"[归档] {p}")
    print(f"[归档] {rp}")
    return 0


def _chapter_memory(ch: int, r: dict) -> str:
    """单章画像记忆条目（【第N章·文风】前缀=编号精确通道锚点）。"""
    head = f"【第{ch}章·文风】"
    if r["total"] == 0:
        return f"{head}病灶0处——端着腔（主谓宾不全/两字半句）已清零，文风干净。"
    body = [f"{head}病灶{r['total']}处（碎片段{r['frags']}·独词句{r['f3']}·短促链{r['f5']}）"
            f"｜综合分{r['score']}｜短促子句占比{r['f1_ratio']}%"]
    for it in r["items"][:MAX_DETAIL_IN_MEMORY]:
        body.append(f"- L{it['line']} [{it['type']}] {it['text']}")
    if r["total"] > MAX_DETAIL_IN_MEMORY:
        body.append(f"- …其余 {r['total'] - MAX_DETAIL_IN_MEMORY} 条见 cortexa-out/style_profile.json")
    return "\n".join(body)


def cmd_sync(root: Path, scope: str) -> int:
    style_scope = f"{scope}-style"
    profile = scan_book(root)
    chapters = profile["chapters"]
    hit = sorted(ch for ch, r in chapters.items() if r["total"] > 0)
    clean = sorted(ch for ch, r in chapters.items() if r["total"] == 0)
    total_items = sum(r["total"] for r in chapters.values())

    overview = (
        f"【文风总览】扫描{len(chapters)}章：有病灶{len(hit)}章、共{total_items}处；干净{len(clean)}章。"
        f"口径：端着腔=故意主谓宾不齐全的简略语句、两个字一个半句；"
        f"分碎片段（独立成段无主语短语）、独词句（行内<=2字独立成句）、短促链（>=3个<=2字子句连排）三类，"
        f"对话行与主谓完整句豁免。"
        f"病灶章清单：{'、'.join(str(c) for c in hit)}。"
        f"干净章清单：{'、'.join(str(c) for c in clean)}。"
        f"逐章明细查询方式：问\u201c第N章的文风\u201d（编号通道直达）。"
        f"更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}；结构化明细：cortexa-out/style_profile.json"
    )

    with Cortexa(data_dir=root / ".cortexa") as cm:
        cm._settings.rule_llm_fallback = False  # 纯规则分流：快、零成本
        cleared = cm.forget_scope(style_scope)  # 幂等刷新：先清旧画像，杜绝新旧叠加
        msgs = [{"role": "user", "content": overview, "timestamp": LOGICAL_T0}]
        resp = cm.remember_messages(msgs, scope=style_scope, session_id="style000")
        stored = resp.stored
        for ch in sorted(chapters):
            msgs = [{"role": "user", "content": _chapter_memory(ch, chapters[ch]),
                     "timestamp": LOGICAL_T0}]
            stored += cm.remember_messages(
                msgs, scope=style_scope, session_id=f"style{ch:03d}").stored
        stats = cm.stats(scope=style_scope)

    # 扫描产物同步落盘（query 命中的明细以 JSON 为权威全量）
    cmd_scan(root)
    print(f"[sync] scope={style_scope}：清旧 {cleared} 条，入库 {stored} 条"
          f"（1 总览 + {len(chapters)} 章画像），库存 {stats['active_memories']} 条")
    return 0


def cmd_query(root: Path, scope: str, question: str, top_k: int) -> int:
    style_scope = f"{scope}-style"
    with Cortexa(data_dir=root / ".cortexa") as cm:
        results = cm.recall(question, scope=style_scope, top_k=top_k)
    if not results:
        print(f"[query] 无命中（style scope 未建库？先跑 sync）：{question}")
        return 1
    print(f"[query] {question!r} -> 命中 {len(results)} 条（scope={style_scope}）\n")
    for i, r in enumerate(results, 1):
        print(f"--- #{i} score={r.score:.3f} ---")
        print(r.memory)
        print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="文风画像记忆维度（scan/sync/query）")
    ap.add_argument("root", help="小说项目根目录（BOOK_ROOT）")
    ap.add_argument("command", choices=["scan", "sync", "query"])
    ap.add_argument("question", nargs="?", default=None, help="query 的提问文本")
    ap.add_argument("--scope", default=None, help="主 scope（默认=目录名，与 novel_ingest 一致）")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"目录不存在：{root}")
        return 1
    scope = args.scope or root.name

    if args.command == "scan":
        return cmd_scan(root)
    if args.command == "sync":
        return cmd_sync(root, scope)
    if not args.question:
        print("query 需要提问文本")
        return 1
    return cmd_query(root, scope, args.question, args.top_k)


if __name__ == "__main__":
    raise SystemExit(main())
