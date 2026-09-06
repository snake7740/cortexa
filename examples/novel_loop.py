"""创作闭环自动化管线：context（写前上下文包）→ 人工/agent 创作 → place（落位+回灌+审计）。

与 novel_bridge.py 的分工：bridge 面向三主体状态与复盘；loop 只服务创作循环，
把"写一章"切成三步自动化，创作本身由 agent 会话或外部 LLM 完成（本脚本不生成正文）。

    python examples/novel_loop.py <BOOK_ROOT> context 108   # 生成写前上下文包
    python examples/novel_loop.py <BOOK_ROOT> place 108 <中转md>  # 章节->正文目录+sync+audit
    python examples/novel_loop.py <BOOK_ROOT> check          # 仅 sync+audit（验证用）

产出全部归位 cortexa-out/（与 bridge 同约定）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

os.environ.pop("OPENAI_API_KEY", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 自足：不依赖 cwd/PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent))

from novel_ingest import ingest_all

SUBJECTS = ("叶长生", "苏晚晴", "秦洛")
ITEMS = ("黑色木牌",)
DIRECTION_GLOB = "故事方向_*.md"  # 多段方向文档（Ch108-127、Ch128-200…）均参与弧线表查找


def _out_dir(root: Path) -> Path:
    d = root / "cortexa-out"
    d.mkdir(exist_ok=True)
    return d


def _arc_row(root: Path, ch: int) -> str | None:
    """从全部方向文档的弧线表提取该章行（按章号降序文档优先，命中即返）。

    检索范围 = cortexa-out/（契约归位处）+ 根目录（历史散落处，B7 归位后仅兜底）。
    """
    bases = [root / "cortexa-out", root]
    docs: list[Path] = []
    for base in bases:
        for p in base.glob(DIRECTION_GLOB):
            if p not in docs:
                docs.append(p)
    docs.sort(reverse=True)
    for doc in docs:
        for line in doc.read_text(encoding="utf-8").splitlines():
            if re.match(rf"^\|\s*{ch}\s*\|", line):
                return line
    return None


def _arc_keywords(row: str | None) -> str:
    """弧线行 -> recall 关键词（取核心事件列，去管道符，截前 16 字）。"""
    if row is None:
        return ""
    cells = [c.strip() for c in row.split("|") if c.strip()]
    if len(cells) < 4:
        return ""
    return re.sub(r"[*`（(].*?[-—]", "", cells[3])[:16].replace(" ", "")


def cmd_context(root: Path, ch: int, semantic: bool = False) -> int:
    scope = root.name
    lines = [f"# 第{ch}章 写前上下文包（{datetime.now():%Y-%m-%d %H:%M}）", ""]

    row = _arc_row(root, ch)
    lines += ["## 1) 弧线目标（故事方向文档）", "", row or "（弧线表无该章行，自由发挥但遵守硬规则）", ""]

    # 上一章结尾衔接（文件名格式不唯一：早期 Ch{N}_标题.md 与后期 Ch{N}_第{N}章.md
    # 并存，按章号前缀 glob 取首个命中，避免硬编码命名静默丢结尾段）
    ch_dir = root / "废灵根"
    prev_matches = sorted(ch_dir.glob(f"Ch{ch - 1}_*.md"))
    if prev_matches:
        tail = prev_matches[0].read_text(encoding="utf-8").strip()[-600:]
        lines += ["## 2) 上一章结尾（衔接用，最后 600 字）", "", "…", tail, ""]

    # 写前 recall（记忆库三查询；--semantic 开启语义通道 = 候选更全/弱题可修，需 openai 端点）
    from cortexa import Cortexa

    lines += ["## 3) 记忆库写前核对（recall" + (" · 语义通道" if semantic else "") + "）", ""]
    _prov = "openai" if semantic else None
    with Cortexa(data_dir=root / ".cortexa", embedding_provider=_prov,
                 semantic_channel=semantic) as cm:
        kw = _arc_keywords(row)
        queries = [f"第{ch - 1}章"] + ([kw] if kw else [])
        for q in queries:
            lines.append(f"### Q: {q}")
            for t in cm.recall_texts(q, scope=scope, top_k=3):
                lines.append(f"- {t[:120]}")
            lines.append("")

    text = "\n".join(lines) + "\n"
    p = _out_dir(root) / f"next_context_Ch{ch}.md"
    p.write_text(text, encoding="utf-8")
    print(f"[上下文包] {p}")
    print(text[:1500])
    return 0


def _run_check(root: Path, label: str) -> str:
    """sync + audit，返回报告文本（供 place/check 共用，逻辑单一定义）。"""
    r = ingest_all(root, root.name)
    rep = None
    from cortexa import Cortexa

    with Cortexa(data_dir=root / ".cortexa") as cm:
        rep = cm.audit(root.name, subjects=list(SUBJECTS), items=ITEMS)
    by_kind: dict[str, int] = {}
    for i in rep.issues:
        by_kind[i.kind] = by_kind.get(i.kind, 0) + 1
    kind_txt = "、".join(f"{k}×{v}" for k, v in sorted(by_kind.items())) or "无"
    return (
        f"# 创作闭环检查（{label}，{datetime.now():%Y-%m-%d %H:%M}）\n\n"
        f"- 新增记忆：**{r['stored']} 条**；库存 {r['active_memories']} 条\n"
        f"- 审计异常：{kind_txt}\n"
        f"- high/high 双高异常：{sum(1 for i in rep.issues if 'high/high' in i.detail)} 项"
        f"（双高 = 两个高置信状态点冲突，真矛盾优先级最高，应为 0）\n"
    )


def _sync_webnovel_state(root: Path, ch: int) -> str | None:
    """B 方案（渐进接管）：落位后把 .webnovel/state.json 进度字段追平到 ch。

    只改 progress.current_chapter/next_chapter/last_updated 与追踪标记；
    富字段（主角状态/伏笔/活跃线/关系）自 Ch106 起未走 webnovel-writer 工作流，
    不自动伪造，首次标 stale_since 说明；无 state.json 返回 None。幂等保守。
    """
    f = root / ".webnovel" / "state.json"
    if not f.is_file():
        return None
    data = json.loads(f.read_text(encoding="utf-8"))
    progress = data.setdefault("progress", {})
    changed: list[str] = []
    if progress.get("current_chapter") != ch:
        progress["current_chapter"] = ch
        changed.append(f"current_chapter={ch}")
    if progress.get("next_chapter") != ch + 1:
        progress["next_chapter"] = ch + 1
        changed.append(f"next_chapter={ch + 1}")
    today = datetime.now().strftime("%Y-%m-%d")
    if progress.get("last_updated") != today:
        progress["last_updated"] = today
        changed.append(f"last_updated={today}")
    for key in ("protagonist_state", "active_strands", "foreshadowing", "relationships"):
        if key in data and isinstance(data[key], dict):
            data[key].setdefault("stale_since", "2026-04-23")
            data[key]["maintained_by"] = "novel_loop(progress-only)"
            changed.append(f"{key}: stale-marked")
    data["state_auto_tracked_from"] = 106
    data["state_auto_tracked_note"] = (
        "progress 字段由 novel_loop --sync-state 追平正文；Ch106 前由 webnovel-writer 维护。"
    )
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return ("state.json 进度追平 → " + "；".join(changed)) if changed else "state.json 已是最新"


def cmd_place(root: Path, ch: int, src: Path, sync_state: bool = False) -> int:
    """章节落位（中转 -> 废灵根/ 规范命名）+ 立即 sync + audit。

    硬门控：字数红线 2000-3000（去空白口径）不达标拒绝落位——
    不合格章节不进正文目录、不进记忆库，避免污染后续全部环节。
    """
    if not src.is_file():
        print(f"中转文件不存在：{src}")
        return 1
    from novel_wordcount import count_chars

    n_chars = count_chars(src.read_text(encoding="utf-8"))
    if not 2000 <= n_chars <= 3000:
        print(f"[拒绝落位] Ch{ch} 字数 {n_chars}，超出红线 2000-3000")
        return 1
    dst = root / "废灵根" / f"Ch{ch}_第{ch}章.md"
    shutil.copy(src, dst)
    print(f"[落位] {dst}（{n_chars} 字，达标）")
    text = _run_check(root, f"Ch{ch} 落位")
    p = _out_dir(root) / f"loop_check_Ch{ch}.md"
    p.write_text(text, encoding="utf-8")
    print(text)
    print(f"[归档] {p}")
    if sync_state:
        report = _sync_webnovel_state(root, ch)
        if report:
            print(f"[{report}]")
    return 0


def cmd_check(root: Path) -> int:
    text = _run_check(root, "纯验证")
    print(text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="创作闭环自动化管线")
    ap.add_argument("root")
    ap.add_argument("command", choices=["context", "place", "check"])
    ap.add_argument("chapter", nargs="?", type=int, default=None)
    ap.add_argument("src", nargs="?", default=None, help="place 模式：中转章节 md 路径")
    ap.add_argument("--semantic", action="store_true",
                    help="写前 recall 开语义通道（需 openai 端点+语义索引；真实写前用）")
    ap.add_argument("--sync-state", action="store_true",
                    help="place 落位后把 .webnovel/state.json 进度字段追平（B 方案渐进接管）")
    a = ap.parse_args()
    root = Path(a.root).expanduser().resolve()
    if not root.is_dir():
        print(f"目录不存在：{root}")
        return 1
    if a.command == "context":
        if a.chapter is None:
            print("context 需要 <章号>")
            return 1
        return cmd_context(root, a.chapter, semantic=a.semantic)
    if a.command == "place":
        if a.chapter is None or a.src is None:
            print("place 需要 <章号> <中转md>")
            return 1
        return cmd_place(root, a.chapter, Path(a.src).expanduser().resolve())
    return cmd_check(root)


if __name__ == "__main__":
    raise SystemExit(main())