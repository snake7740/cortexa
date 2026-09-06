"""手写闭环管线的底层实现（重建版：_tmp_manual_loop.py 的唯一被依赖方）。

职责拆成三块，全部委托 examples/ 既有实现，避免逻辑双份：
- build_context(ch)  -> novel_loop.cmd_context（弧线行+上一章结尾+写前 recall）
- place_one(ch)      -> novel_loop.cmd_place（字数硬门控 2000-3000 -> 落位 -> sync -> audit）
- lint_body(text)    -> 红线前置检查（一句段确定性并入、破折号、AI 味高频词），与落位门控同口径

历史教训：检查后置造成"写了挡、挡了改、改了再挡"的循环；自动修复必须是确定性规则，
不引入语义猜测（叙述性一句段并入前段，对话段/标题行豁免）。
"""
from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "examples"))

import novel_loop as nl  # noqa: E402
from novel_wordcount import count_chars  # noqa: E402

import os
BOOK_ROOT = Path(os.environ.get("CORTEXA_BOOK_ROOT", r"C:\Users\junzh\新建文件夹\废灵根修仙-重构版"))
TRANSIT = BOOK_ROOT / "cortexa-out"  # 产出归位到小说项目，不再落 cortexa-mvp/cortexa-out/

_AI_TASTE = ("缓缓说道", "沉声道", "淡淡地说", "嘴角勾起", "眼中闪过一丝")
_TITLE_RE = re.compile(r"^#")


def build_context(ch: int) -> str:
    """生成写前上下文包并返回全文（novel_loop 同时归档到小说根目录 cortexa-out/）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        nl.cmd_context(BOOK_ROOT, ch)
    out = TRANSIT / f"next_context_Ch{ch}.md"
    return out.read_text(encoding="utf-8")


def place_one(ch: int) -> tuple[bool, str]:
    """中转章节落位（硬门控+sync+audit），返回 (是否通过, 摘要)。"""
    src = TRANSIT / f"_transit_Ch{ch}.md"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = nl.cmd_place(BOOK_ROOT, ch, src)
    report = BOOK_ROOT / "cortexa-out" / f"loop_check_Ch{ch}.md"
    info = buf.getvalue().strip().splitlines()
    stored = next((ln for ln in info if "新增记忆" in ln), "")
    arch = report.read_text(encoding="utf-8") if report.is_file() else ""
    hi = next((ln for ln in arch.splitlines() if "双高" in ln), "")
    return code == 0, f"{stored}；{hi}（详情 {report.name}）"


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[。！？])", text) if s.strip()]


def lint_body(text: str, fix: bool = False) -> tuple[str, list[str], bool]:
    """红线检查：一句段（叙述性）、破折号、AI 味高频词。

    fix=True 时，叙述性一句段确定性并入前段（前段优先）；对话段（引号开头）与标题行豁免。
    返回 (可能修过的文本, 报告列表, 是否通过)。报告含"已并入"的视为已自动修复，不阻断。
    """
    lines = text.splitlines()
    reports: list[str] = []
    if fix:
        out: list[str] = []
        for ln in lines:
            stripped = ln.strip()
            is_body = stripped and not _TITLE_RE.match(stripped) and not stripped.startswith(("“", "\""))
            if is_body and out and len(_sentences(stripped)) == 1:
                prev_idx = next((i for i in range(len(out) - 1, -1, -1) if out[i].strip()), None)
                if prev_idx is not None and not _TITLE_RE.match(out[prev_idx].strip()):
                    out[prev_idx] = out[prev_idx].rstrip() + stripped
                    reports.append(f"一句段已并入前段：{stripped[:18]}…")
                    continue
                reports.append(f"一句段无法并入（前无正文段）：{stripped[:18]}…")
            out.append(ln)
        text = "\n".join(out)
    else:
        for ln in lines:
            stripped = ln.strip()
            if (
                stripped
                and not _TITLE_RE.match(stripped)
                and not stripped.startswith(("“", "\""))
                and len(_sentences(stripped)) == 1
            ):
                reports.append(f"一句段：{stripped[:18]}…")
    dash = text.count("——")
    if dash:
        reports.append(f"破折号×{dash}")
    for w in _AI_TASTE:
        if w in text:
            reports.append(f"AI 味词汇：{w}")
    n = count_chars(text)
    if not 2000 <= n <= 3000:
        reports.append(f"字数 {n} 超出红线 2000-3000")
    ok = all("已并入" in r for r in reports)
    return text, reports, ok
