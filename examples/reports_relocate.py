"""B7 修复：小说根目录散落的 Cortexa 报告归位到 cortexa-out/（产出归位契约）。

背景：_integration_map.md 契约 = Cortexa 产出单点归档 BOOK_ROOT/cortexa-out/，但审计发现
status_latest.md / sync_*.md / audit_*.md / loop_check_*.md(101) / next_context_*.md(9) /
故事方向_*.md(3) 全部散在根目录 → 「复盘 30 秒路径」入口断链。

安全设计：
- 只移动明确前缀的 Cortexa 产出文件，绝不碰正文/设定/大纲/.webnovel/.cortexa；
- 目标已存在则改名（_dupN）不覆盖；默认 dry-run；--apply 移动并写归位报告。

用法：
    python examples/reports_relocate.py <小说根目录>          # dry-run
    python examples/reports_relocate.py <小说根目录> --apply  # 执行
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

PREFIXES = ("loop_check_Ch", "next_context_Ch", "故事方向_", "sync_", "audit_")
EXACT = {"status_latest.md"}


def _matches(name: str) -> bool:
    return name in EXACT or name.startswith(PREFIXES)


def main() -> int:
    ap = argparse.ArgumentParser(description="Cortexa 报告归位 cortexa-out（默认 dry-run）")
    ap.add_argument("root")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    root = Path(a.root).expanduser().resolve()
    out = root / "cortexa-out"
    cands = sorted(p for p in root.iterdir() if p.is_file() and _matches(p.name))
    print(f"根目录待归位文件：{len(cands)} 个（目标：{out}/）")
    for p in cands:
        print(f"   {p.name}")
    if not a.apply:
        print("\n[dry-run] 未移动。加 --apply 执行（同名冲突自动 _dupN，不覆盖）。")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for p in cands:
        dst = out / p.name
        n = 1
        while dst.exists():
            dst = out / f"{p.stem}_dup{n}{p.suffix}"
            n += 1
        shutil.move(str(p), str(dst))
        moved.append(f"{p.name} -> cortexa-out/{dst.name}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report = out / f"relocate_report_{stamp}.md"
    report.write_text(
        "# Cortexa 报告归位执行记录\n\n"
        f"> 时间 {stamp} | 移动 {len(moved)} 个文件到 cortexa-out/（契约单点归档）\n\n"
        + "\n".join(f"- {m}" for m in moved) + "\n",
        encoding="utf-8",
    )
    print(f"[已归位] {len(moved)} 个；执行报告：{report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
