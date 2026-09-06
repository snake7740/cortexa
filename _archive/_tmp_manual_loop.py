"""手写闭环辅助：ctx <ch> 落盘写前上下文包；lint <ch> 红线前置检查；place <ch> 走落位硬门控。
执笔者与管线解耦：本脚本只做管线，不做生成。
标准流程：ctx → 执笔 → lint（--fix 自动合并一句段，字数不足则扩充实质内容）→ place。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(r"c:\Cortexa\cortexa-mvp")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "examples"))

import _tmp_batch_write as bw  # noqa: E402  复用 build_context / place_one / lint_body
from novel_wordcount import count_chars  # noqa: E402


def cmd_lint(ch: int, fix: bool) -> int:
    src = bw.TRANSIT / f"_transit_Ch{ch}.md"
    text = src.read_text(encoding="utf-8")
    new_text, reports, ok = bw.lint_body(text, fix=fix)
    if fix and new_text != text:
        src.write_text(new_text, encoding="utf-8")
        text = new_text
    n = count_chars(text)
    dash = text.count("——")
    print(f"lint Ch{ch}: 字数 {n}（红线 2000-3000）" + (f"  破折号×{dash}" if dash else ""))
    if n < 2000:
        print(f"  ✗ 偏短：需扩充 {2000 - n} 字实质内容（侦察细节/人物刻画/口供深化）")
    elif n > 3000:
        print(f"  ✗ 偏长：需删减 {n - 3000} 字")
    for r in reports:
        print(f"  {'✗' if ok is False or '已并入' not in r else '·'} {r}")
    passed = ok and dash == 0 and 2000 <= n <= 3000
    print(f"lint 结论：{'PASS，可 place' if passed else '待修正后重跑 lint'}")
    return 0 if passed else 2


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--fix"]
    fix = "--fix" in sys.argv[1:]
    if len(args) != 2 or args[0] not in ("ctx", "lint", "place"):
        print("usage: _tmp_manual_loop.py ctx|lint|place <ch> [lint 支持 --fix]")
        return 1
    ch = int(args[1])
    if args[0] == "ctx":
        ctx = bw.build_context(ch)
        out = REPO / f"_tmp_ctx_Ch{ch}.txt"
        out.write_text(ctx, encoding="utf-8")
        print(f"ctx written: {out} ({len(ctx)} chars)")
        return 0
    if args[0] == "lint":
        return cmd_lint(ch, fix)
    passed, info = bw.place_one(ch)
    print(f"place Ch{ch}: {'PASS' if passed else 'WARN/FAIL'} | {info}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
