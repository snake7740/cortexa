"""B1 修复：把小说 scope 内被误标 CODE_DEBUG/CODE_DEV 的记忆重归类（2026-09-06 审计发现）。

背景：C:/Cortexa 审计发现小说库 .cortexa/cortexa.db 中 41 条设定/正文内容
（content 以【设定名】【第N章·】锚点开头）被标成 CODE_DEBUG(40)/CODE_DEV(1)——
类型分层统计失真。判定锚点 = content 以「【」开头（小说摄取统一锚点格式）。

安全设计（防误伤）：
- 只处理 scope == 书根目录名 且 mem_type IN (CODE_DEBUG, CODE_DEV) 的行；
- content 不以【 开头的行不处理并单独报告（不猜）；
- 默认 dry-run：只打印将改清单，零写入；
- --apply 前自动备份 db 到 <book>/.cortexa/backups/cortexa_db_pre_typefix_<ts>.db；
- 只 UPDATE memories.mem_type，不改内容/哈希/时间轴。

用法：
    python examples/novel_type_fix.py <小说根目录>                 # dry-run
    python examples/novel_type_fix.py <小说根目录> --apply --type RULE  # 应用（备份后）
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

MISLABELED = ("CODE_DEBUG", "CODE_DEV")


def _conn(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    return con


def main() -> int:
    ap = argparse.ArgumentParser(description="novel scope CODE 误标重归类（默认 dry-run）")
    ap.add_argument("root", help="小说根目录（含 .cortexa/cortexa.db）")
    ap.add_argument("--apply", action="store_true", help="实际写库（先自动备份）")
    ap.add_argument("--type", default="FACT", choices=["FACT", "RULE"],
                    help="重归类目标类型（默认 FACT：无特殊语义最安全）")
    a = ap.parse_args()
    root = Path(a.root).expanduser().resolve()
    db = root / ".cortexa" / "cortexa.db"
    if not db.is_file():
        print(f"[中止] 找不到记忆库：{db}")
        return 2
    scope = root.name
    con = _conn(db)
    rows = con.execute(
        "SELECT id, mem_type, content FROM memories "
        "WHERE scope=? AND mem_type IN (?,?) ORDER BY id",
        (scope, MISLABELED[0], MISLABELED[1]),
    ).fetchall()
    anchored = [r for r in rows if r["content"].startswith("【")]
    other = [r for r in rows if not r["content"].startswith("【")]
    print(f"scope={scope}  误标候选 {len(rows)} 条（CODE_DEBUG/CODE_DEV）")
    print(f"  - 锚点明确（以【开头，可安全归类）: {len(anchored)} 条")
    print(f"  - 锚点不明（不动，需人工核对）    : {len(other)} 条")
    for r in other:
        print(f"      !! {r['id']} [{r['mem_type']}] {r['content'][:50]!r}")
    if not a.apply:
        print("\n[dry-run] 目标类型 =", a.type, "；未写库。加 --apply 执行（自动备份）。")
        for r in anchored[:10]:
            print(f"   -> {r['id']} {r['mem_type']} -> {a.type}: {r['content'][:36]}")
        if len(anchored) > 10:
            print(f"   ... 共 {len(anchored)} 条（此处显示前 10）")
        con.close()
        return 0

    if not anchored:
        print("[dry-run 结论] 无锚点明确行，无需 --apply")
        con.close()
        return 0
    backup_dir = root / ".cortexa" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"cortexa_db_pre_typefix_{stamp}.db"
    shutil.copy2(db, backup)
    print(f"[备份] {backup} ({backup.stat().st_size} bytes)")
    con.execute("BEGIN")
    for r in anchored:
        con.execute(
            "UPDATE memories SET mem_type=? WHERE id=? AND scope=?",
            (a.type, r["id"], scope),
        )
    con.commit()
    print(f"[已应用] {len(anchored)} 条 {MISLABELED} -> {a.type}（scope={scope}）")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
