# -*- coding: utf-8 -*-
"""按章号+正则在 _staging 中定位原文行，输出 行号|全文，供整改取锚点。"""
import re
from pathlib import Path

STAGING = Path(__file__).parent / "_staging"
targets = {
    11: r"等了大约一个时辰|事实确认了|阴大人|明天夜里|但后山|后山。夜晚",
    12: r"等了大约一刻钟|脚步声，两道|王宣没有再反驳|突破了炼气四层|主殿。青云宗",
    13: r"不值得赌|太近了|所以他赌前者|筑基期。|身后的追杀者停了|三天。|还有一件事要做|很轻。踩|疼。嘴|不对。他|滴答",
}
for ch, pat in targets.items():
    fn = STAGING / f"Ch{ch}_第{ch}章.md"
    print(f"===== Ch{ch}")
    lines = fn.read_text(encoding="utf-8").splitlines()
    for i, l in enumerate(lines):
        if re.search(pat, l) and len(l.strip()) < 200:
            prev = lines[i - 1] if i > 0 else ""
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            print(i + 1, "|", l[:110])
            if not prev.strip():
                print("    前:", lines[i - 2][-80:] if i >= 2 else "")
            if not nxt.strip():
                print("    后:", lines[i + 2][:80] if i + 2 < len(lines) else "")
