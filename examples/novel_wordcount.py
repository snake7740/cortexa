"""章节字数红线检查：每章正文 2000-3000 字（去空白字符口径）。

用法：
    python examples/novel_wordcount.py <小说根目录> [--min 2000] [--max 3000]

定位：创作闭环的基本检查（与 novel_loop.py check 同级）。新章落位前必查；
全量跑用于体检存量。退出码：0=全部达标，1=存在超界章节（供管线硬门控）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def count_chars(text: str) -> int:
    """字数口径：去除全部空白字符后的长度（中英文混排统一口径）。"""
    return len(re.sub(r"\s", "", text))


def check(root: Path, lo: int = 2000, hi: int = 3000) -> list[tuple[int, str, int, str]]:
    """返回 [(章号, 文件名, 字数, 状态)]，状态 ∈ 达标/偏短/偏长。"""
    ch_dir = next((root / d for d in ("废灵根", "正文", "chapters") if (root / d).exists()), None)
    if ch_dir is None:
        raise SystemExit(f"未找到章节目录：{root}")
    files = sorted(
        ch_dir.glob("Ch*_*.md"),
        key=lambda p: int(re.match(r"Ch(\d+)", p.name).group(1)),
    )
    rows: list[tuple[int, str, int, str]] = []
    for f in files:
        n = int(re.match(r"Ch(\d+)", f.name).group(1))
        c = count_chars(f.read_text(encoding="utf-8"))
        status = "达标" if lo <= c <= hi else ("偏短" if c < lo else "偏长")
        rows.append((n, f.name, c, status))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="章节字数红线检查（2000-3000）")
    ap.add_argument("root", type=Path)
    ap.add_argument("--min", type=int, default=2000)
    ap.add_argument("--max", type=int, default=3000)
    args = ap.parse_args()

    rows = check(args.root, args.min, args.max)
    bad = [r for r in rows if r[3] != "达标"]
    nums = [r[2] for r in rows]
    print(f"章节总数: {len(rows)}  字数范围: {min(nums)}-{max(nums)}  "
          f"均值 {sum(nums) // len(nums)}")
    print(f"达标({args.min}-{args.max}): {len(rows) - len(bad)}/{len(rows)}")
    for n, name, c, status in bad:
        print(f"  ✗ Ch{n}（{name}）: {c} 字 [{status}]")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
