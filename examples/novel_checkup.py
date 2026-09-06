"""小说记忆库查体（"学习委员"交作业）：五科检查一次出成绩单。

用法：
    python examples/novel_checkup.py <小说根目录> [scope] [-o 输出文件]

-o 直接写 UTF-8 文件（绕开 PowerShell -NoProfile 通道的 GBK 管道，防乱码根因修复）。

五科：地板复验 / 孤岛统计 / 衰减健康度 / 探针自测（11 题机检）/ 一致性审计。
只评不改——报告异常清单，修正动作属 P4 登记项（误修比漏报伤）。
纯本地：零 AI API 依赖（hash 嵌入 + 确定性检查器）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 自足：不依赖 cwd/PYTHONPATH

os.environ.pop("OPENAI_API_KEY", None)  # 查体纯本地：防环境变量把嵌入切到失联端点导致挂起（2026-09-01 实锤）

from cortexa import Cortexa

# 11 题探针（前 8 题与 novel_probe.py 同题）+ 机检期望子串：
# 口径 = 期望子串进入 top3 任一命中（覆盖度，非答案精度——诚实标注）。
# 期望子串取自既有命中证据；弱探针（⑥跨词汇）允许不通过，报告会如实暴露。
# ⑨章号类探针验证编号精确通道（实际使用痛点，2026-08-31 落地）。
# ⑩⑪新卷探针（2026-09-01）：用词汇对齐口径机检新卷记忆存在性（锚点经对抗性复核验证）；
# 自然语言问法的跨词汇检索属已知弱点（同⑥，语义嵌入提分场景），在 novel_probe 清单中如实暴露。
CHECK_PROBES: list[tuple[str, str]] = [
    ("叶长生的灵根是什么", "灵根"),
    ("因果之眼的代价是什么", "因果之眼"),
    ("苏晚晴是谁", "苏晚晴"),
    ("叶长生怎么进的天枢宗", "天枢宗"),
    ("幽冥教在做什么", "幽冥教"),
    ("叶长生住在哪", "甲字"),  # 弱探针：跨词汇检索，未过=语义嵌入提分场景
    ("万兽山脉有什么", "万兽山脉"),
    ("全书主线是什么", "主线"),
    ("第107章发生了什么", "【第107章"),  # ⑨章号类：编号精确通道（章节锚点）
    ("守碑人是谁", "守碑"),  # ⑩新卷：Ch175/176 守碑人锚点（词汇对齐口径）
    ("落星湖的规矩", "落星湖"),  # ⑪新卷：Ch170/176 落星湖锚点（词汇对齐口径）
]

SUBJECTS = ("叶长生",)
ITEMS = ("古虚戒|戒",)  # 代称在场口径，与审计修复同源（2026-09-01）


def main() -> None:
    argv = sys.argv[1:]
    out_path = None
    if "-o" in argv:
        i = argv.index("-o")
        out_path = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    root = Path(argv[0]) if argv else Path.cwd()
    scope = argv[1] if len(argv) > 1 else root.name

    with Cortexa(data_dir=root / ".cortexa") as cm:
        r = cm.checkup(
            scope, subjects=SUBJECTS, items=ITEMS, probes=CHECK_PROBES, top_k=3
        )

    lines: list[str] = []
    emit = lines.append
    emit(f"=== 查体成绩单：{scope} ===")
    emit(f"库存: {r.active_memories} 条")
    emit(f"[地板] 复验 {r.floor_total} 条，违规 {r.floor_violations}"
         f"{'（异常！写入红线被绕过）' if r.floor_violations else '（正常）'}")
    emit(f"[孤岛] 曾被检索命中 {r.orphans['ever_hit']} 条，"
         f"孤岛率 {r.orphans['orphan_ratio']:.1%}（口径：未被问过≠检索不到）")
    d = r.decay
    emit(f"[衰减] 检查 {d['checked']} 条（RULE/no_decay 豁免）：未复习 "
         f"{d['never_reviewed']} / 健康 {d['healthy']} / 老化 {d['aged']}")
    p = r.probes
    emit(f"[自测] 探针 {p['hits']}/{p['total']}（{p['rate']:.0%}）")
    for item in p["details"]:
        mark = "✓" if item["hit"] else "✗"
        emit(f"   {mark} {item['query']}（期望含「{item['expect']}」）")
    a = r.audit
    if a is not None:
        issues = a["issues"]
        emit(f"[审计] 追踪 {len(a['subjects'])} 实体 / 物品，异常 {len(issues)} 处"
             "（只报告不修正）")
        for it in issues[:5]:
            emit(f"   - {it['kind']} @ {it['between']}: {it['detail'][:60]}")
        if len(issues) > 5:
            emit(f"   ... 其余 {len(issues) - 5} 处")
    if out_path:
        Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"report written: {out_path}")
    else:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
