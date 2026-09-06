"""小说场景 · 生命周期自动结算（novel 实证验证工具）。

输入：正文章节文件；输出：角色生命周期状态报告（落小说项目 cortexa-out/）。

机制：消费 L3 novel schema + L2 LifecycleEngine——出场空白>阈值自动判 DORMANT，
后续再现判 REINTRODUCED（带"无交代回归"警告）；死亡仅 NARRATION 语境驱动 EXITED。
这是把图分析暴露的"周元白 64 章无交代离场"从人肉发现变成引擎自动判定。

用法：python examples/novel_lifecycle_settle.py <小说根目录>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cortexa.domain import CastRegistry, LifecycleEngine
from cortexa.domain.facts import FactExtractor
from cortexa.schema import get_schema

_CH_DIRS = ("废灵根", "正文", "chapters")


def chno_of(fname) -> int:
    name = fname.name if hasattr(fname, "name") else str(fname)
    m = re.search(r"Ch(\d+)", name)
    return int(m.group(1)) if m else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="生命周期自动结算（DORMANT/REINTRODUCED）")
    ap.add_argument("root", type=Path)
    ap.add_argument("--threshold", type=int, default=10, help="出场空白多少章判 DORMANT")
    args = ap.parse_args()

    ch_dir = next((args.root / d for d in _CH_DIRS if (args.root / d).exists()), None)
    if ch_dir is None:
        print(f"章节目录不存在: {args.root}", file=sys.stderr)
        return 1

    schema = get_schema("novel")
    engine = LifecycleEngine(schema)
    cast = CastRegistry(schema)
    # 角色名单从 schema.characters + aliases 读（Cast 驱动，去硬编码）
    characters = cast.known_names()
    # 归一化名单：统计/状态机用主名，别名出现归入主名
    def _norm(name: str) -> str:
        return cast.canonicalize(name)

    # 每角色收集"活体在场"章节（语境分层 + Cast 归一：别名出现归入主名）
    appear: dict[str, list[int]] = defaultdict(list)
    fx = FactExtractor(schema)
    # 归一：先查主名，再查别名（同名不同写法算同一人）
    canon_pool = [n for n in characters if not cast.is_alias(n)]
    for f in sorted(ch_dir.glob("Ch*_第*章.md"), key=chno_of):
        ch = chno_of(f)
        text = f.read_text(encoding="utf-8")
        for c in canon_pool:
            if fx.is_present(text, c, canon_pool):
                appear[_norm(c)].append(ch)
        # 别名出现也归入主名（如"长生"归"叶长生"）
        for alias, canonical in schema.aliases.items():
            if alias in text and alias not in canon_pool:
                appear[_norm(canonical)].append(ch)

    # 死亡先验：死者 = "名 + 0-3字 + 死亡词"紧邻主语模式（防周元白被"赵铁柱死了...周元白带来"误判）
    # 架构依据：死亡状态先验回喂出场判定——EXITED 后的出现除非复活表述否则视为引用
    death_words = schema.event_predicates.get("death", ())
    if death_words and canon_pool:
        _subj_death_re = re.compile(
            "(" + "|".join(re.escape(c) for c in canon_pool) + ")"
            r"[^，。！？\n]{0,4}(?:" + "|".join(re.escape(w) for w in death_words) + ")"
        )
    else:
        _subj_death_re = None
    exited_at: dict[str, int] = {}
    if _subj_death_re:
        for f in sorted(ch_dir.glob("Ch*_第*章.md"), key=chno_of):
            ch = chno_of(f)
            text = f.read_text(encoding="utf-8")
            for m in _subj_death_re.finditer(text):
                subj = _norm(m.group(1))  # 归一到主名
                before = text[max(0, m.start() - 6):m.start()]
                if re.search(r"(?:说|道|问|想|记得|听说)$", before):
                    continue
                exited_at[subj] = min(exited_at.get(subj, ch), ch)

    # 状态机结算：遍历 appear 键（已 Cast 归一的主名）
    report = []
    for c in sorted(appear.keys(), key=lambda x: -len(appear[x])):
        chs = sorted(appear[c])
        if not chs:
            continue
        # 首次出场即 ACTIVE（INTRODUCED 是注册态，出场即在场）
        state = "ACTIVE"
        events = []
        # 死亡先验：ch 晚于该角色死亡章的"出场"视为死者引用，不推进状态机
        _dead_before = exited_at.get(c)
        chs_alive = [x for x in chs if _dead_before is None or x < _dead_before]
        if len(chs_alive) < len(chs):
            events.append({"ch": _dead_before, "ev": "death", "to": "EXITED",
                           "gap": 0, "note": "死亡(EXITED)——后续出现视为引用"})
        chs = chs_alive
        if not chs:
            continue
        last = chs[0]
        for ch in chs[1:]:
            gap = ch - last
            if gap > args.threshold:
                # 离场空白超阈值 -> 用引擎判定 depart: ACTIVE->DORMANT
                r1 = engine.apply_event("depart", state, context="NARRATION")
                if r1.ok:
                    events.append({"ch": last, "ev": "depart", "to": "DORMANT",
                                   "gap": gap, "note": "自动离场"})
                    state = "DORMANT"
                # 再出现 -> 用引擎判定 return_: DORMANT->REINTRODUCED
                r2 = engine.apply_event("return_", state, context="NARRATION")
                if r2.ok:
                    events.append({"ch": ch, "ev": "return_", "to": "REINTRODUCED",
                                   "gap": gap, "note": "⚠️无交代回归"})
                    state = "REINTRODUCED"
            last = ch
        if events:
            report.append({"name": c, "first": chs[0], "last": chs[-1],
                           "appear": len(chs), "events": events})

    # 输出
    out_dir = args.root / "cortexa-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    # markdown 报告
    L = ["# 角色生命周期自动结算（LifecycleEngine）", "",
         f"> 分析时间 {stamp} | schema: {schema.name} | 阈值: {args.threshold} 章", ""]
    for r in sorted(report, key=lambda x: -x["appear"]):
        L.append(f"## {r[chr(110)+chr(97)+chr(109)+chr(101)]} (ch{r[chr(102)+chr(105)+chr(114)+chr(115)+chr(116)]}-ch{r[chr(108)+chr(97)+chr(115)+chr(116)]}, {r[chr(97)+chr(112)+chr(112)+chr(101)+chr(97)+chr(114)]}章)")
        for e in r["events"]:
            L.append(f"- ch{e[chr(99)+chr(104)]}: {e[chr(101)+chr(118)]} -> {e[chr(116)+chr(111)]} (空白{e[chr(103)+chr(97)+chr(112)]}章) {e[chr(110)+chr(111)+chr(116)+chr(101)]}")
        L.append("")
    md = "\n".join(L)
    f1 = out_dir / f"lifecycle_settle_{stamp}.md"
    f1.write_text(md, encoding="utf-8")
    (out_dir / "lifecycle_settle_latest.md").write_text(md, encoding="utf-8")
    # json 明细
    f2 = out_dir / f"lifecycle_settle_{stamp}.json"
    f2.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "lifecycle_settle_latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告: {f1}")
    print(f"明细: {f2}")
    flagged = [r["name"] for r in report if any("回归" in e["note"] for e in r["events"])]
    if flagged:
        print(f"⚠️ 无交代回归角色: {flagged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())