"""三主体创作环路桥：webnovel-writer（创作流）↔ 小说项目（内容资产）↔ Cortexa（记忆）。

创作环路（验收场景：写 1万/5万/10万字后记忆依然可复盘、不混乱）：
    /webnovel-write 新章 -> 废灵根/ChNNN.md            [创作流产出]
      -> sync    幂等摄取进 .cortexa                    [记忆输入]
      -> recall  写前查设定/前情（facade.recall）        [记忆输出反哺创作]
      -> audit   状态线/矛盾/物品缺口（混乱度量化）      [记忆健康检查]
      -> 产出归位 cortexa-out/                          [单点复盘]

解耦契约（三主体互不侵入）：
- 不 import webnovel-writer 代码：只读 PROJECT_ROOT/.webnovel/state.json（文件契约）；
- 不修改外部插件：webnovel-writer 经 --project-root 或 .claude/.webnovel-current-project
  指针定位本书（指针为纯文本绝对路径，_is_project_root 判据 = .webnovel/state.json 存在）；
- Cortexa 产出只落 BOOK_ROOT/cortexa-out/：sync/audit/status 报告单点归档，正文与
  .webnovel/ 一概不动，复盘从 cortexa-out 单入口开始。

用法：
    python examples/novel_bridge.py <BOOK_ROOT> status
    python examples/novel_bridge.py <BOOK_ROOT> sync
    python examples/novel_bridge.py <BOOK_ROOT> audit [--subject 名 ...] [--item 物品 ...]
        [--scope S]（默认 scope = BOOK_ROOT 目录名，与 novel_ingest 一致）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# 桥默认走本地 hash 嵌入（与 novel_ingest 同策略：业务验证零 API）
os.environ.pop("OPENAI_API_KEY", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 自足：不依赖 cwd/PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 复用同目录 novel_ingest

from novel_ingest import ingest_all

DEFAULT_SUBJECTS = ("叶长生", "苏晚晴", "秦洛")
DEFAULT_ITEMS = ("黑色木牌",)


def _ch_no(name: str) -> int:
    """章号提取（章节数字序，非文件名字典序——避免 Ch9 > Ch107 的假最新）。"""
    m = re.match(r"Ch(\d+)_", name)
    return int(m.group(1)) if m else 0


def _out_dir(root: Path) -> Path:
    d = root / "cortexa-out"
    d.mkdir(exist_ok=True)
    return d


def _write_out(root: Path, name: str, text: str) -> Path:
    p = _out_dir(root) / name
    p.write_text(text, encoding="utf-8")
    return p


def _read_state(root: Path) -> dict | None:
    f = root / ".webnovel" / "state.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def _chapter_dir(root: Path) -> Path | None:
    for cand in ("废灵根", "正文", "chapters"):
        d = root / cand
        if d.exists() and list(d.glob("Ch*_*.md")):
            return d
    return None


# ---------------------------------------------------------------------------
# status：三主体健康度快照（创作环路的复盘入口）
# ---------------------------------------------------------------------------

def cmd_status(root: Path, scope: str) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [f"# 三主体健康度快照（{now}）", ""]

    # 1) 内容资产
    ch_dir = _chapter_dir(root)
    lines += ["## 1) 内容资产", ""]
    if ch_dir is None:
        lines.append("- 正文：未找到章节目录（废灵根/正文/chapters）")
        chapters = []
    else:
        chapters = sorted(ch_dir.glob("Ch*_*.md"), key=lambda p: _ch_no(p.name))
        settings_n = len(list((root / "设定集").rglob("*.md"))) if (root / "设定集").exists() else 0
        outline_n = len(list((root / "大纲").glob("*.md"))) if (root / "大纲").exists() else 0
        lines += [
            f"- 正文：`{ch_dir.name}/` 共 {len(chapters)} 章（最新 {chapters[-1].name if chapters else '无'}）",
            f"- 设定集 {settings_n} 个 md、大纲 {outline_n} 个 md",
        ]
    stray = [p.name for p in root.glob("Ch*_*.md")]  # 根目录历史副本（权威在子目录）
    if stray:
        lines.append(f"- 注意：根目录另有 {len(stray)} 个 Ch*.md 历史副本（权威在 `{ch_dir.name}/`，不参与摄取）")
    lines.append("")

    # 2) webnovel-writer（只读文件契约）
    lines += ["## 2) webnovel-writer（.webnovel/state.json）", ""]
    st = _read_state(root)
    if st is None:
        lines.append("- 未初始化（无 state.json）；创作流可用 `--project-root` 显式指定本书")
    else:
        info = st.get("project_info", {})
        prog = st.get("progress", {})
        hero = st.get("protagonist_state", {})
        lines += [
            f"- 书名：{info.get('title', '?')}（{info.get('genre', '?')}）",
            f"- 进度：第 {prog.get('current_chapter', '?')} 章完成，下一章 {prog.get('next_chapter', '?')}"
            f"（更新于 {prog.get('last_updated', '?')}）",
            f"- 主角状态：{hero.get('name', '?')} | 修为 {hero.get('power', '?')} | 位置 {hero.get('location', '?')}",
            f"- 活跃线 {len(st.get('active_threads', []))} 条、伏笔 {len(st.get('foreshadowing', []))} 条、"
            f"关系 {len(st.get('relationships', {}))} 人",
            f"- 节奏（Strand）：当前 {st.get('strand_tracker', {}).get('current_dominant', '?')}，"
            f"建议下一步 {st.get('strand_tracker', {}).get('next_recommended', '?')}",
        ]
    lines.append("")

    # 3) Cortexa 记忆库 + 记忆健康度（mini-audit）
    lines += ["## 3) Cortexa 记忆库（.cortexa/）", ""]
    if not (root / ".cortexa" / "cortexa.db").is_file():
        lines.append("- 未摄取：先运行 `sync` 建库")
    else:
        from cortexa import Cortexa

        with Cortexa(data_dir=root / ".cortexa") as cm:
            stats = cm.stats(scope=scope)
            lines.append(f"- 记忆条数：{stats.get('active_memories', '?')}（scope={scope}）")
            try:
                orphan = cm.journal.orphan_stats(scope=scope)
                lines.append(
                    f"- 检索覆盖：累计命中 {orphan['ever_hit']} 条，孤岛 {orphan['orphans']} 条"
                    f"（{orphan['orphan_ratio']:.0%}，未被问过≠检索不到）"
                )
            except Exception:
                pass
            rep = cm.audit(scope, subjects=list(DEFAULT_SUBJECTS), items=DEFAULT_ITEMS)
            by_kind: dict[str, int] = {}
            for i in rep.issues:
                by_kind[i.kind] = by_kind.get(i.kind, 0) + 1
            kind_txt = "、".join(f"{k}×{v}" for k, v in sorted(by_kind.items())) or "无"
            lines.append(f"- 记忆混乱度（{len(DEFAULT_SUBJECTS)} 实体审计异常）：{kind_txt}")
            lines.append("- 最近 sync/audit 报告见 `cortexa-out/`（异常详情跑 `audit` 子命令）")
    lines.append("")

    # 4) 产出归位
    lines += ["## 4) 产出归位（cortexa-out/）", ""]
    outs = sorted(_out_dir(root).glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    lines += [f"- {p.name}" for p in outs[:8]] or ["-（空）"]
    if len(outs) > 8:
        lines.append(f"- …共 {len(outs)} 个文件")

    text = "\n".join(lines) + "\n"
    print(text)
    p = _write_out(root, "status_latest.md", text)
    print(f"[归档] {p}")
    return 0


# ---------------------------------------------------------------------------
# sync：正文/设定 -> 记忆库（幂等；stored=0 即记忆库与正文严格一致）
# ---------------------------------------------------------------------------

def cmd_sync(root: Path, scope: str) -> int:
    r = ingest_all(root, scope)
    if "error" in r:
        print(r["error"])
        return 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# 同步报告（{now}）",
        "",
        f"- scope：`{r['scope']}`；数据目录：`{r['data_dir']}`",
        f"- 正文：{r['chapters']} 章；设定：{len(r['settings'])} 个文件",
        f"- 本次新增：**{r['stored']} 条**（0 = 记忆库与正文严格一致，幂等验证通过）",
        f"- 库存：{r['active_memories']} 条；耗时 {r['elapsed_s']}s",
        "",
        "## 设定文件明细",
        "",
    ]
    lines += [f"- {f['file']}: +{f['stored']} 条" for f in r["settings"]]
    text = "\n".join(lines) + "\n"
    print(text)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    p = _write_out(root, f"sync_{stamp}.md", text)
    print(f"[归档] {p}")
    return 0


# ---------------------------------------------------------------------------
# audit：一致性审计，报告归位 cortexa-out/
# ---------------------------------------------------------------------------

def _render_audit(rep, items: tuple[str, ...]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    L = [
        f"# 一致性审计报告（{now}）",
        "",
        f"- scope：`{rep.scope}`；实体：{'、'.join(rep.subjects)}；物品：{'、'.join(items) or '无'}",
        "",
    ]
    for s, line in rep.state_lines.items():
        L += [f"## {s}：{len(line)} 个状态点", "", "| 章 | 值 | 置信 | 证据 |", "|---|---|---|---|"]
        L += [f"| Ch{p.chapter} | {p.value} | {p.confidence} | {p.evidence} |" for p in line]
        L.append("")
    L += [f"## 异常清单（{len(rep.issues)} 项）", ""]
    L += [f"- **[{i.kind}]** {i.between}：{i.detail}" for i in rep.issues] or ["- 无"]
    return "\n".join(L) + "\n"


def cmd_audit(root: Path, scope: str, subjects: tuple[str, ...], items: tuple[str, ...]) -> int:
    from cortexa import Cortexa

    with Cortexa(data_dir=root / ".cortexa") as cm:
        rep = cm.audit(scope, subjects=list(subjects), items=tuple(items))
    text = _render_audit(rep, items)
    print(text)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    p = _write_out(root, f"audit_{stamp}.md", text)
    print(f"[归档] {p}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="三主体创作环路桥（webnovel-writer ↔ 小说项目 ↔ Cortexa）")
    ap.add_argument("root", help="小说项目根目录（BOOK_ROOT）")
    ap.add_argument("command", choices=["status", "sync", "audit"])
    ap.add_argument("--scope", default=None, help="记忆 scope（默认 = 目录名，与 novel_ingest 一致）")
    ap.add_argument("--subject", action="append", default=None,
                    help="审计实体（可多次；默认 叶长生/苏晚晴/秦洛，与 status 同口径）")
    ap.add_argument("--item", action="append", default=None,
                    help="审计物品（可多次；默认 黑色木牌，与 status 同口径）")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"目录不存在：{root}")
        return 1
    scope = args.scope or root.name

    if args.command == "status":
        return cmd_status(root, scope)
    if args.command == "sync":
        return cmd_sync(root, scope)
    # 默认口径与 cmd_status 的 DEFAULT_SUBJECTS/ITEMS 对齐：否则 status 快照与
    # audit 报告两套混乱度数字不可比（review 2026-08-31 C-4）
    subjects = tuple(args.subject) if args.subject else DEFAULT_SUBJECTS
    items = tuple(args.item) if args.item else DEFAULT_ITEMS
    return cmd_audit(root, scope, subjects, items)


if __name__ == "__main__":
    raise SystemExit(main())
