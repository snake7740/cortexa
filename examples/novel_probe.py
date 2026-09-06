"""小说记忆库探针验证：带标准答案的检索质量核对。

用法：
    python examples/novel_probe.py <小说根目录> [问题 ...] [-o 输出文件]
不传问题时运行内置探针组（答案来自设定集，可人工核对）。
-o 直接写 UTF-8 文件（绕开 PowerShell -NoProfile 通道的 GBK 管道，防乱码根因修复）。

纯本地验证：hash 嵌入 + FTS bigram，零 AI API 依赖。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 自足：不依赖 cwd/PYTHONPATH

os.environ.pop("OPENAI_API_KEY", None)  # 探针纯本地：防环境变量把嵌入切到失联端点导致挂起（2026-09-01 实锤）

from cortexa import Cortexa

# 内置探针：问题 -> 核对要点（答案依据 = 设定集/正文已知事实）
PROBES: list[tuple[str, str]] = [
    ("叶长生的灵根是什么", "期望：废灵根/灵根废物设定"),
    ("因果之眼的代价是什么", "期望：金手指设计的代价条款"),
    ("苏晚晴是谁", "期望：女主卡·身份与来历"),
    ("叶长生怎么进的天枢宗", "期望：Ch24 录取 / Ch40 终审第7名"),
    ("幽冥教在做什么", "期望：反派设计·渗透各宗"),
    ("叶长生住在哪", "期望：茅屋 / 甲字十二号"),
    ("万兽山脉有什么", "期望：Ch50 地理志线索"),
    ("全书主线是什么", "期望：总纲/世界观一句话"),
]

# 新卷探针组（Ch128-176 卷四事实，2026-09-01 补；答案依据=正文锚点，对抗性复核已验）
PROBES_NEW_VOLUME: list[tuple[str, str]] = [
    ("建域碑是什么", "期望：守碑/域心/初代（Ch175 建域史）"),
    ("谁在替楼守夜", "期望：看星人/守更人（Ch176 落星湖）"),
    ("沈阴在巷口说了什么", "期望：棺里那位/落星湖见（Ch176 章末钩子）"),
    ("星盘楼什么时候出现", "期望：初七/落星湖（Ch176 楼现形规则）"),
    ("苏晚晴的头发怎么了", "期望：白发（卷四反噬线）"),
]


def main() -> None:
    argv = sys.argv[1:]
    out_path: str | None = None
    if "-o" in argv:
        i = argv.index("-o")
        out_path = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    root = Path(argv[0])
    scope = argv[1] if len(argv) > 1 else root.name
    queries = [(q, "") for q in argv[2:]] or PROBES + PROBES_NEW_VOLUME

    lines: list[str] = []
    emit = lines.append

    with Cortexa(data_dir=root / ".cortexa") as cm:
        emit(f"记忆库存：{cm.stats(scope=scope)['active_memories']} 条")
        emit("")
        for query, note in queries:
            emit(f"Q: {query}")
            if note:
                emit(f"   [{note}]")
            hits = cm.recall(query, scope=scope, top_k=3)
            if not hits:
                emit("   （无命中）")
            for r in hits:
                emit(f"   {r.score:.4f}  {r.memory[:80]}")
            emit("")

    if out_path:
        Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"report written: {out_path}")
    else:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
