"""D2 dry-run：小说库实体标签噪音审计（只读，2026-09-06）。

产出：标签质量分类统计 + 清洗候选清单，供决策（清洗 = P4 中文专名 NER 的前置地基；
当前实体通道已实证关闭，清洗无即时召回收益 → 只留档不应用，不写 R2）。

判定规则（与 encoding/词表纪律一致）：
- 干净标签：2-8 字纯 CJK、无标点/空格/ASCII/对话碎片；
- 噪音标签：含标点（。，？！：；…、“”〔〕【】·）或 ASCII/空白，或长度越界；
- 可规整：纯 CJK 但以句读结尾（如「叶长生。」→ 去尾后 叶长生 保留；去掉后长度<2 则丢弃如「对。」→对）。

输出写入审计报告文件；本脚本不改任何数据。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

_CJK = re.compile(r"^[\u4e00-\u9fff]+$")
_TRAIL = re.compile(r"[。，？！：；、…“”‘’\u2026]+$")


def classify(tag: str) -> tuple[str, str]:
    """返回 (类别, 建议)。类别: keep|noise|normalize。"""
    if 2 <= len(tag) <= 8 and _CJK.match(tag):
        return "keep", tag
    stripped = _TRAIL.sub("", tag)
    if 2 <= len(stripped) <= 8 and _CJK.match(stripped):
        return "normalize", stripped
    return "noise", None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", help="cortexa.db 路径（只读）")
    ap.add_argument("-o", "--out", default="")
    a = ap.parse_args()
    con = __import__("sqlite3").connect("file:" + str(Path(a.db).as_posix()) + "?mode=ro", uri=True)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT id, entities_json FROM memories WHERE scope=? AND status='ACTIVE'",
        ("废灵根修仙-重构版",),
    ).fetchall()
    stats: Counter = Counter()
    total_tags = 0
    keep_hist: Counter = Counter()
    noise_examples: list[str] = []
    normalize_pairs: list[tuple[str, str]] = []
    mem_with_clean = 0
    gold = {"叶长生", "苏晚晴", "周元白", "沈渊", "恒远", "秦洛", "韩执事", "沈阴", "孟知秋",
            "方桐", "陈元吉", "赵云飞", "王宣", "赵铁柱", "铁柱", "面具长老", "周暮", "林远",
            "宗主", "宋远", "持戒者", "老执事", "灰衣人", "叶巨川"}
    gold_hit: Counter = Counter()
    for mem_id, ej in rows:
        try:
            tags = json.loads(ej or "[]")
        except Exception:
            continue
        if not isinstance(tags, list):
            continue
        clean_here = 0
        for tg in tags:
            if not isinstance(tg, str) or not tg:
                continue
            total_tags += 1
            kind, sugg = classify(tg)
            stats[kind] += 1
            if kind == "keep":
                keep_hist[tg] += 1
                clean_here += 1
                if tg in gold:
                    gold_hit[tg] += 1
            elif kind == "normalize":
                normalize_pairs.append((tg, sugg))
                keep_hist[sugg] += 1
                clean_here += 1
                if sugg in gold:
                    gold_hit[sugg] += 1
            else:
                if len(noise_examples) < 15 and tg not in noise_examples:
                    noise_examples.append(tg)
        if clean_here:
            mem_with_clean += 1
    con.close()
    lines = [
        "# 实体标签质量审计（D2 dry-run，2026-09-06）",
        "",
        f"- 活跃记忆：{len(rows)}；实体标签总数：{total_tags}",
        f"- 分类：keep={stats['keep']} / normalize(可规整)={stats['normalize']} / noise(噪音)={stats['noise']}",
        f"- 含至少一个干净标签的记忆：{mem_with_clean}（{mem_with_clean / max(len(rows),1):.0%}）",
        f"- 规整后词表去重规模：{len(keep_hist)}（原始含噪音）",
        "- 金标角色命中（24 名主角/重要角色在规整词表中出现的个数/总频次）：",
        *[f"    {g}: {gold_hit.get(g, 0)}" for g in sorted(gold)],
        "",
        "## 噪音样例（前 15，仅展示）",
        *[f"- {e!r}" for e in noise_examples],
        "",
        "## 可规整样例（前 15：去掉句读后保留）",
        *[f"- {a!r} -> {b!r}" for a, b in normalize_pairs[:15]],
        "",
        "## 结论与处置",
        "- 判定：标签噪音主要来自抽取残留（对白句读/短句碎片），污染词表统计与未来 NER 地基；",
        "- 决策：清洗是 **P4 中文专名 NER 的前置任务**；当前实体通道已实证关闭（9/11 均等、3.7x 延迟），",
        "  清洗无即时召回收益 → 本报告与 tools 留档，不在本批次写 R2（应用时先备份再跑 novel_entity_clean --apply）。",
    ]
    text = "\n".join(lines) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print("report:", a.out)
    else:
        print(text[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
