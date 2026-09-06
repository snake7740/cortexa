"""记忆库一致性审计（正式 API 版）：对标作者 consistency_checker.py。

用法：
    python examples/novel_consistency.py <小说根目录> [实体 ...] [--item 物品 ...] [-o 输出文件]

实体/物品缺省时使用《废灵根修仙》实测配置（叶长生/苏晚晴/秦洛 + 黑色木牌）。
-o 直接写 UTF-8 文件（绕开 PowerShell -NoProfile 通道的 GBK 管道，防乱码根因修复）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 自足：不依赖 cwd/PYTHONPATH

from cortexa import Cortexa

DEFAULT_SUBJECTS = ("叶长生", "苏晚晴", "秦洛")
DEFAULT_ITEMS = ("黑色木牌",)


def main() -> None:
    argv = sys.argv[1:]
    args: list[str] = []
    item_list: list[str] = []
    out_path: str | None = None
    expect_item = False
    expect_out = False
    for a in argv:
        if a == "--item":
            expect_item = True
        elif a == "-o":
            expect_out = True
        elif expect_item:
            item_list.append(a)
            expect_item = False
        elif expect_out:
            out_path = a
            expect_out = False
        else:
            args.append(a)
    root = Path(args[0]) if args else Path.cwd()
    subjects = tuple(args[1:]) or DEFAULT_SUBJECTS
    items = tuple(item_list) or DEFAULT_ITEMS

    lines: list[str] = []
    emit = lines.append

    with Cortexa(data_dir=root / ".cortexa") as cm:
        report = cm.audit(root.name, subjects=subjects, items=items)

        for subject, line in report.state_lines.items():
            emit(f"== {subject} 状态线（{len(line)} 个状态点）==")
            for p in line:
                flag = "" if p.confidence == "high" else "  (low)"
                emit(f"  Ch{p.chapter:>3}: {p.value}{flag}")
            emit("")

        emit(f"== 一致性异常：{len(report.issues)} 处 ==")
        by_kind: dict[str, list] = {}
        for i in report.issues:
            by_kind.setdefault(i.kind, []).append(i)
        for kind, lst in by_kind.items():
            emit(f"  [{kind}] {len(lst)} 处")
            for i in lst:
                emit(f"    {i.between}: {i.detail}")
        if not report.issues:
            emit("  （无异常）")

    if out_path:
        Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"report written: {out_path}")
    else:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
