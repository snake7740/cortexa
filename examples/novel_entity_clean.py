"""D2 工具：小说库实体标签规整（dry-run 默认；--apply 前自动备份，不写 R2 直到授权）。

规则（与 novel_entity_audit.py 同源）：
- keep：2-8 字纯 CJK；
- normalize：去句读尾缀后为 2-8 字纯 CJK（如 「叶长生。」→叶长生）；
- noise：其余（含标点中缀/ASCII/超长/对白碎片）→ 移除；
只改 memories.entities_json，不动 content/hash/时间轴。

用法：
    python examples/novel_entity_clean.py <db路径> [--scope 废灵根修仙-重构版]      # dry-run
    python examples/novel_entity_clean.py <db路径> --apply                         # 备份后应用
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_CJK = re.compile(r"^[\u4e00-\u9fff]+$")
_TRAIL = re.compile(r"[。，？！：；、…“”‘’\u2026]+$")


def clean_tag(tag: str) -> str | None:
    if not isinstance(tag, str) or not tag:
        return None
    if 2 <= len(tag) <= 8 and _CJK.match(tag):
        return tag
    stripped = _TRAIL.sub("", tag)
    if 2 <= len(stripped) <= 8 and _CJK.match(stripped):
        return stripped
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--scope", default="废灵根修仙-重构版")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    db = Path(a.db)
    if not db.is_file():
        print(f"[中止] 找不到库：{db}")
        return 2
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, entities_json FROM memories WHERE scope=? AND status='ACTIVE'", (a.scope,)
    ).fetchall()
    changed: list[tuple[str, list[str]]] = []
    removed_noise = 0
    normalized = 0
    for r in rows:
        try:
            tags = json.loads(r["entities_json"] or "[]")
        except Exception:
            continue
        if not isinstance(tags, list):
            continue
        cleaned: list[str] = []
        dirty = False
        for tg in tags:
            if tg in cleaned:
                dirty = True
                continue
            out = clean_tag(tg)
            if out is None:
                removed_noise += 1
                dirty = True
            elif out != tg:
                normalized += 1
                cleaned.append(out)
                dirty = True
            else:
                cleaned.append(tg)
        if dirty:
            changed.append((r["id"], cleaned))
    print(f"scope={a.scope} 记忆 {len(rows)}；将变更 {len(changed)} 条；移除噪音 {removed_noise}；规整 {normalized} 个标签")
    if not a.apply:
        print("[dry-run] 未写库。加 --apply 执行（自动备份到同目录 backups/）。")
        return 0
    backup_dir = db.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"cortexa_db_pre_entityclean_{stamp}.db"
    shutil.copy2(db, backup)
    print(f"[备份] {backup}")
    con.executemany(
        "UPDATE memories SET entities_json=? WHERE id=? AND scope=?",
        [(json.dumps(c, ensure_ascii=False), mid, a.scope) for mid, c in changed],
    )
    con.commit()
    print(f"[已应用] {len(changed)} 条 entities_json 已规整")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
