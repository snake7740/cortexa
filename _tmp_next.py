# -*- coding: utf-8 -*-
"""按 style_scan_detail.md 的明细，输出指定章节命中行的原文+前后邻行，供整改取锚点。
用法: python _tmp_next.py 15 16 17   或   python _tmp_next.py 15-29
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).parent
STAGING = BASE / "_staging"
DETAIL = BASE / "cortexa-out" / "style_scan_detail.md"

def parse_args():
    chs = []
    for a in sys.argv[1:]:
        if "-" in a:
            lo, hi = a.split("-")
            chs.extend(range(int(lo), int(hi) + 1))
        else:
            chs.append(int(a))
    return chs

def load_detail():
    """返回 {章号: [(行号, 类型)]}"""
    data = {}
    cur = None
    for line in DETAIL.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^## Ch(\d+)（共", line)
        if m:
            cur = int(m.group(1))
            data[cur] = []
            continue
        m = re.match(r"^- L(\d+) \[(?:碎片·(\w+)|独词句|短促链)", line)
        if m and cur is not None:
            data[cur].append((int(m.group(1)), m.group(2) or line.split("[")[1].split("]")[0]))
    return data

def main():
    detail = load_detail()
    for ch in parse_args():
        items = detail.get(ch, [])
        if not items:
            print(f"===== Ch{ch} 无明细")
            continue
        fn = STAGING / f"Ch{ch}_第{ch}章.md"
        if not fn.exists():
            print(f"===== Ch{ch} [未入staging] 明细{len(items)}条")
            continue
        lines = fn.read_text(encoding="utf-8").splitlines()
        seen = set()
        print(f"===== Ch{ch}（明细{len(items)}条）")
        for ln, kind in items:
            if ln in seen:
                continue
            seen.add(ln)
            i = ln - 1
            if i >= len(lines):
                print(f"L{ln} [{kind}] <越界>")
                continue
            prev = lines[i - 1] if i > 0 else ""
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            print(f"L{ln} [{kind}] {lines[i][:130]}")
            if not prev.strip():
                print("    前:", lines[i - 2][-100:] if i >= 2 else "")
            if not nxt.strip():
                print("    后:", lines[i + 2][:100] if i + 2 < len(lines) else "")

if __name__ == "__main__":
    main()
