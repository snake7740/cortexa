"""NER v1 规则回扫工具（2026-09-06，P4 前置试点；dry-run 默认，--apply 需先备份）。

思路：实体抽取欠覆盖（金标 24 中多数角色标签频次≈0）→ 用「高置信种子词表」对既有库
内容做规则回扫补标，零 LLM、确定性：
  种子 = 金标角色 24 ∪ 规整后高频纯 CJK 专名(freq>=3) ∪ entity_states 已注册名；
对每条 ACTIVE 记忆 content 命中的种子去重追加进 entities（保留既有标签顺序，追加在后；
单条上限 8 防膨胀）。只改 entities_json。

用法：
    python examples/novel_entity_backfill.py <db路径> [--scope 废灵根修仙-重构版]   # dry-run
    python examples/novel_entity_backfill.py <db路径> --apply                        # 备份后应用
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

_CJK = re.compile(r"^[\u4e00-\u9fff]+$")
_TRAIL = re.compile(r"[。，？！：；、…“”‘’\u2026]+$")
GOLD = {"叶长生", "苏晚晴", "周元白", "沈渊", "恒远", "秦洛", "韩执事", "沈阴", "孟知秋",
        "方桐", "陈元吉", "赵云飞", "王宣", "赵铁柱", "铁柱", "面具长老", "周暮", "林远",
        "宗主", "宋远", "持戒者", "老执事", "灰衣人", "叶巨川"}
MAX_PER_MEM = 8


def _norm(tag: str) -> str | None:
    if not isinstance(tag, str) or not tag:
        return None
    if 2 <= len(tag) <= 8 and _CJK.match(tag):
        return tag
    s = _TRAIL.sub("", tag)
    if 2 <= len(s) <= 8 and _CJK.match(s):
        return s
    return None


def _seeds(con: sqlite3.Connection, scope: str) -> list[str]:
    cur = con.cursor()
    freq: Counter = Counter()
    rows = cur.execute(
        "SELECT entities_json FROM memories WHERE scope=? AND status='ACTIVE'", (scope,)
    ).fetchall()
    for (ej,) in rows:
        try:
            tags = json.loads(ej or "[]")
        except Exception:
            continue
        if isinstance(tags, list):
            for tg in tags:
                n = _norm(tg)
                if n:
                    freq[n] += 1
    seeds = set(GOLD)
    for n, f in freq.items():
        if f >= 3 and len(seeds) < 300:
            seeds.add(n)
    try:
        for (name,) in cur.execute(
            "SELECT entity_name FROM entity_states WHERE scope=?", (scope,)
        ).fetchall():
            if name and len(name) <= 8:
                seeds.add(name)
    except sqlite3.OperationalError:
        pass
    # 长名优先，防短名(如 铁柱)抢先吞并长名（内容命中判定用最长匹配优先排序仍可）
    return sorted(seeds, key=lambda s: (-len(s), s))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--scope", default="废灵根修仙-重构版")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    db = Path(a.db)
    if not db.is_file():
        print(f"[中止] {db} 不存在")
        return 2
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    seeds = _seeds(con, a.scope)
    print(f"种子词表: {len(seeds)}（金标24 ∪ 高频规整 ∪ entity_states）")
    rows = con.execute(
        "SELECT id, content, entities_json FROM memories WHERE scope=? AND status='ACTIVE'", (a.scope,)
    ).fetchall()
    changed: list[tuple[str, list[str]]] = []
    added_total = 0
    gold_before = sum(1 for r in rows if any(_norm(t) in GOLD for t in (json.loads(r["entities_json"] or "[]") if isinstance(json.loads(r["entities_json"] or "[]"), list) else [])))
    gold_after = 0
    for r in rows:
        try:
            cur_tags = json.loads(r["entities_json"] or "[]")
        except Exception:
            cur_tags = []
        if not isinstance(cur_tags, list):
            continue
        existing = [_norm(t) for t in cur_tags if _norm(t)]
        have = set(existing)
        added: list[str] = []
        for s in seeds:
            if s in r["content"] and s not in have and len(added) < MAX_PER_MEM:
                added.append(s)
        if added:
            changed.append((r["id"], existing + added))
            added_total += len(added)
        gold_after += bool((have | set(added)) & GOLD)
    print(f"记忆 {len(rows)}；将补标 {len(changed)} 条；新增标签 {added_total} 个")
    print(f"含金标角色的记忆: 回扫前 {gold_before}（{gold_before/max(len(rows),1):.0%}）→ 回扫后 {gold_after}（{gold_after/max(len(rows),1):.0%}）")
    if not a.apply:
        print("[dry-run] 未写库。--apply 前自动备份。")
        con.close()
        return 0
    backup_dir = db.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"cortexa_db_pre_backfill_{stamp}.db"
    shutil.copy2(db, backup)
    con.executemany(
        "UPDATE memories SET entities_json=? WHERE id=? AND scope=?",
        [(json.dumps(c, ensure_ascii=False), mid, a.scope) for mid, c in changed],
    )
    con.commit()
    print(f"[已应用] {len(changed)} 条；备份 {backup}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())