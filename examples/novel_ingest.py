"""小说全量摄取脚本：把一本网文（设定集/大纲/正文）灌入 Cortexa 记忆库。

用法：
    python examples/novel_ingest.py <小说根目录> [scope]

小说根目录约定（本项目的 Obsidian vault 结构）：
    <root>/设定集/**.md   -> 设定层记忆（session=settings）
    <root>/大纲/*.md      -> 大纲记忆（session=outline）
    <root>/废灵根/Ch*_*.md -> 正文章节（session=chNNN）

特性：
- 默认本地 hash 嵌入（零网络依赖，中文检索以 FTS bigram 为主力通道）；
  如需语义嵌入质量，自行在代码中传 openai_api_key（需接受网络延迟）
- 纯规则分流（rule_llm_fallback=False）：零 LLM 成本、摄取快
- 章节条目带【第N章·标题】前缀锚定；设定按 ## 小节拆条
- 数据落在 <root>/.cortexa/（跟随小说项目）
- 幂等：content_hash 去重，重跑不会产生重复记忆
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

# 避免外部环境的 OPENAI_API_KEY 被意外感知（本脚本刻意使用本地 hash 嵌入）
os.environ.pop("OPENAI_API_KEY", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 自足：不依赖 cwd/PYTHONPATH

from cortexa import Cortexa
from cortexa.modules.consistency import CHAPTER_INTERVAL_MS, LOGICAL_T0  # 时间轴唯一同源定义

T0 = LOGICAL_T0  # 逻辑时间轴基准（ms）：设定层；章节 = T0 + 章号 * CHAPTER_INTERVAL_MS
MAX_CHUNK = 260  # 正文拆条目标长度（字）
MAX_SET_CHUNK = 500  # 设定拆条上限（字）


def iter_md_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.rglob("*.md") if not p.name.startswith("."))


def split_md_sections(text: str) -> list[str]:
    """设定文档按 ## 小节拆条，超长小节按段落二次合并。"""
    parts = re.split(r"\n(?=## )", text)
    chunks: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) <= MAX_SET_CHUNK:
            chunks.append(part)
            continue
        buf = ""
        for line in part.splitlines():
            line = line.strip()
            if not line:
                continue
            if buf and len(buf) + len(line) + 1 > MAX_SET_CHUNK:
                chunks.append(buf)
                buf = line
            else:
                buf = f"{buf}\n{line}" if buf else line
        if buf:
            chunks.append(buf)
    return chunks


def split_chapter(text: str, ch_no: int, title: str) -> list[str]:
    """正文按段落合并拆条，每条带章节锚定前缀。"""
    prefix = f"【第{ch_no}章·{title}】"
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if buf and len(buf) + len(line) + 1 > MAX_CHUNK:
            chunks.append(f"{prefix}{buf}")
            buf = line
        else:
            buf = f"{buf}{line}" if buf else line
    if buf:
        chunks.append(f"{prefix}{buf}")
    return chunks


def parse_chapter_name(name: str) -> tuple[int, str] | None:
    m = re.match(r"Ch(\d+)_(.+)\.md", name)
    if not m:
        return None
    title = re.sub(r"^第\d+章", "", m.group(2)).strip(" _-")
    return int(m.group(1)), title


def ingest_all(root: Path, scope: str) -> dict:
    """全量幂等摄取：设定集+大纲+正文 -> <root>/.cortexa/。

    返回结构化结果；novel_bridge.py 复用本实现（摄取逻辑单一定义），
    main() 只负责把结果渲染成人读输出。
    """
    result: dict = {
        "scope": scope,
        "data_dir": str(root / ".cortexa"),
        "settings": [],
        "chapters": 0,
        "stored": 0,
        "active_memories": 0,
        "elapsed_s": 0.0,
        "journal": {},
    }
    started = time.perf_counter()
    with Cortexa(data_dir=root / ".cortexa") as cm:
        cm._settings.rule_llm_fallback = False  # 纯规则分流：快、零成本
        total = 0

        # ---- 1) 设定层 ----
        for md in iter_md_files(root / "设定集") + iter_md_files(root / "大纲"):
            text = md.read_text(encoding="utf-8")
            chunks = split_md_sections(text)
            msgs = [
                {"role": "user", "content": f"【{md.stem}】{c}", "timestamp": T0}
                for c in chunks
            ]
            resp = cm.remember_messages(msgs, scope=scope, session_id="settings")
            total += resp.stored
            result["settings"].append({"file": md.name, "stored": resp.stored})

        # ---- 2) 正文章节 ----
        ch_dir = None
        for cand in ("废灵根", "正文", "chapters"):
            d = root / cand
            if d.exists() and list(d.glob("Ch*_*.md")):
                ch_dir = d
                break
        if ch_dir is None:
            result["error"] = "未找到章节目录（Ch*_*.md）"
            return result

        chapter_files = sorted(
            ch_dir.glob("Ch*_*.md"),
            key=lambda p: parse_chapter_name(p.name)[0],
        )
        result["chapters"] = len(chapter_files)
        for md in chapter_files:
            parsed = parse_chapter_name(md.name)
            if parsed is None:
                continue
            ch_no, title = parsed
            text = md.read_text(encoding="utf-8")
            chunks = split_chapter(text, ch_no, title)
            ts = T0 + ch_no * CHAPTER_INTERVAL_MS
            msgs = [{"role": "user", "content": c, "timestamp": ts} for c in chunks]
            resp = cm.remember_messages(msgs, scope=scope, session_id=f"ch{ch_no:03d}")
            total += resp.stored

        result["stored"] = total
        result["active_memories"] = cm.stats(scope=scope)["active_memories"]
        result["journal"] = cm.journal.summary(scope=scope)["by_event_type"]

    result["elapsed_s"] = round(time.perf_counter() - started, 1)
    return result


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    scope = sys.argv[2] if len(sys.argv) > 2 else root.name
    r = ingest_all(root, scope)
    if "error" in r:
        print(r["error"])
        return
    for f in r["settings"]:
        print(f"[设定] {f['file']}: +{f['stored']} 条")
    print(
        f"[正文] 共 {r['chapters']} 章，新增 {r['stored']} 条，库存 {r['active_memories']} 条，"
        f"总耗时 {r['elapsed_s']}s，数据目录 {r['data_dir']}"
    )
    for et, info in r["journal"].items():
        print(f"  journal {et}: {info['count']} 次")


if __name__ == "__main__":
    main()
