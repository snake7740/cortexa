"""G1 工具：为既有库构建语义索引（vectors_sem，分 provider，增量幂等）。

用法：
    python examples/semantic_index_build.py <db路径> [--scope X] [--env .env] [--limit N]
    # 只建缺失行（增量）；默认 provider 从 --env/.env 读，要求 embedding_provider=openai
    # （DeepSeek /embeddings 实测可用；hash provider 会报错提示）。
    # 例（真实使用，先备份库再跑）：python examples/semantic_index_build.py <db> --apply
dry-run：只报将构建行数/预估耗时/维度。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--scope", default=None, help="缺省自动取库内最大 ACTIVE scope")
    ap.add_argument("--env", default="")
    ap.add_argument("--limit", type=int, default=0, help=">0 只构建前 N 条（冒烟）")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    db = Path(a.db)
    if not db.is_file():
        print(f"[中止] {db}")
        return 2
    env = Path(a.env) if a.env else (Path.cwd() / ".env")
    from cortexa.config import Settings
    from cortexa.llm import LLMClient
    from cortexa.storage import SQLiteMemoryStore

    # 强制 openai：.env 默认 hash（真实使用离线基线），建语义索引是显式动作
    s = Settings(_env_file=str(env) if env.is_file() else None, embedding_provider="openai")
    con = sqlite3.connect("file:" + db.as_posix() + "?mode=ro", uri=True)
    cur = con.cursor()
    scope = a.scope
    if not scope:
        scope = cur.execute(
            "SELECT scope FROM memories WHERE status='ACTIVE' GROUP BY scope ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()[0]
    mems = cur.execute(
        "SELECT id, content FROM memories WHERE scope=? AND status='ACTIVE' ORDER BY created_ts_ms", (scope,)
    ).fetchall()
    con.close()
    if a.apply:
        # 2026-09-06：正式库建索引前先备份
        import shutil as _sh
        from datetime import datetime as _dt
        bdir = db.parent / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        _sh.copy2(db, bdir / ("cortexa_db_pre_semindex_" + _dt.now().strftime("%Y%m%d-%H%M%S") + ".db"))
    store = SQLiteMemoryStore(db_path=str(db), embedding_dim=s.embedding_dim)
    existing = {
        r[0] for r in store.connection.execute("SELECT mem_id FROM vectors_sem WHERE scope=?", (scope,))
    }
    todo = [(mid, txt) for mid, txt in mems if mid not in existing]
    if a.limit > 0:
        todo = todo[: a.limit]
    est = len(todo) * 0.65
    print(f"scope={scope} 语义索引：已存在 {len(existing)}，将构建 {len(todo)}，预估 {est/60:.1f} min（~0.65s/条）dim={s.embedding_dim}")
    if not a.apply:
        store.close()
        print("[dry-run] 未写库。--apply 执行（增量幂等）。")
        return 0
    llm = LLMClient(s)
    t0 = time.perf_counter()
    for i in range(0, len(todo), 8):
        batch = todo[i : i + 8]
        try:
            vecs = llm.embed([txt for _, txt in batch])
        except Exception as e:  # noqa: BLE001
            print(f"[中断] embed 失败于 {i}: {type(e).__name__} {str(e)[:120]}")
            break
        for (mid, _txt), vec in zip(batch, vecs):
            store.add_semantic(mid, scope, vec)
        if (i // 8) % 50 == 0:
            print(f"  ... {min(i + 8, len(todo))}/{len(todo)}（{(time.perf_counter()-t0)/60:.1f} min）")
    store.close()
    print(f"[完成] {len(todo)} 条；总耗时 {(time.perf_counter()-t0)/60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())