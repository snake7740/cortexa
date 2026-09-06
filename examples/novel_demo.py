"""小说撰写场景端到端演示：Cortexa 以进程内类库方式嵌入写作流程。

运行：python examples/novel_demo.py
数据写入临时目录、退出时自动清理，不留验证残留。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 自足：不依赖 cwd/PYTHONPATH

from cortexa import Cortexa


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cortexa-demo-"))
    novel = "novel-demo"

    with Cortexa(data_dir=tmp) as cm:
        # ---- 1) 写作前：作者录入人物事件 / 背景事实 / 写作规则 ----
        seeds = [
            "沈屹的怀表是林晚在第三章交给他的，表盖内侧刻着一个晚字",  # EVENT
            "沈屹左手虎口有一道旧疤，是十四岁那年救林晚时留下的",  # EVENT
            "林晚害怕密闭空间，这个弱点源于童年的一场火灾",  # FACT
            "本书视角：除闪回章外统一采用林晚的第一人称",  # RULE
            "第20章之前禁止揭露沈屹的真实身份",  # RULE
        ]
        for text in seeds:
            resp = cm.remember(text, scope=novel)
            print(f"[写入] stored={resp.stored}  {text[:22]}…")

        # ---- 2) 写作中：写到怀表情节，先检索旧线索保证前后一致 ----
        print("\n[检索] 第12章要写怀表特写，查它的来历——")
        for r in cm.recall("怀表的来历是什么", scope=novel, top_k=3):
            print(f"  score={r.score:.4f}  {r.memory[:34]}")

        print("\n[检索] 章节审校：确认身份揭露的时点约束——")
        for r in cm.recall("什么时候可以揭露沈屹的身份", scope=novel, top_k=3):
            print(f"  score={r.score:.4f}  {r.memory[:34]}")

        # ---- 3) 任务流水：进行什么记忆任务、做到了哪些 ----
        print("\n[流水] journal.summary ——")
        for et, info in cm.journal.summary(scope=novel)["by_event_type"].items():
            print(f"  {et}: {info['count']} 次")

        print("\n[流水] orphan_stats 孤岛线索（写入后从未被检索命中的记忆）——")
        stats = cm.journal.orphan_stats(scope=novel)
        print(
            f"  活跃记忆 {stats['active_memories']} 条，曾被命中 {stats['ever_hit']} 条，"
            f"孤岛 {stats['orphans']} 条（比例 {stats['orphan_ratio']:.0%}）"
        )

        print("\n[强化] 检索命中的记忆 FSRS 巩固等级已自动 +1（测试效应）")

    shutil.rmtree(tmp, ignore_errors=True)  # 自清理，不留验证残留
    print("\n演示完成，临时数据已清理。")


if __name__ == "__main__":
    main()
