"""R2 维护：清理 vectors 表混维行（dim<>512，hash 512 为权威；2026-09-06 审计发现）。

历史误用 openai provider 追加过 1024 行 → 与 512 矩阵共存会造成 vector_search 维数错乱。
只删 dim<>512（保留 512）；先备份。dry-run 默认。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--keep-dim", type=int, default=512)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    db = Path(a.db)
    if not db.is_file():
        print(f"[中止] {db}")
        return 2
    con = sqlite3.connect(str(db))
    bad = con.execute(
        "SELECT dim, COUNT(*) FROM vectors WHERE dim<>? GROUP BY dim", (a.keep_dim,)
    ).fetchall()
    total = con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    print(f"vectors 总数 {total}；混维待清理 {bad}（保留 dim={a.keep_dim}）")
    if not a.apply:
        con.close()
        print("[dry-run] 未写库。--apply 前自动备份。")
        return 0
    backup_dir = db.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"cortexa_db_pre_vecfix_{stamp}.db"
    shutil.copy2(db, backup)
    n = con.execute("DELETE FROM vectors WHERE dim<>?", (a.keep_dim,)).rowcount
    con.commit()
    con.close()
    print(f"[已应用] 删除 {n} 行混维向量；备份 {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
