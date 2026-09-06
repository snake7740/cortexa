"""R3-1 A/B：小说真实库上的实体通道去留裁判（2026-09-06）。

做法：把 .cortexa/cortexa.db 复制到临时目录（每个变体一份，互不污染），
用 novel_checkup.CHECK_PROBES（11 题机检，期望子串进 top3 即中）分别评测：
  A) channel off（当前默认基线）
  B) channel on（现有 store.entity_search：存储实体标签命中）
  C) 内容-专名通道原型（content LIKE 专名词表：不求存储标签，直接查正文含名）
输出：逐题命中/时延 + 汇总；结论写报告文件（不自动改默认，供决策）。

纯本地 hash 嵌入；rare_enabled=False 隔离检索通道本身；journal 不落。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os_env_note = None  # placeholder

from novel_checkup import CHECK_PROBES  # noqa: E402

from cortexa.config import Settings  # noqa: E402
from cortexa.llm import LLMClient  # noqa: E402
from cortexa.modules.search import search_memory  # noqa: E402
from cortexa.storage import SQLiteMemoryStore  # noqa: E402

RE = __import__("re")


def _cjk_names(store: SQLiteMemoryStore, scope: str, min_freq: int = 2) -> list[str]:
    """从存储实体标签清洗出纯 CJK 专名词表（2-6 字、去标点/对白碎片、频次>=2）。"""
    import json
    from collections import Counter

    c: Counter = Counter()
    rows = store.connection.execute(
        "SELECT entities_json FROM memories WHERE scope=? AND status='ACTIVE'", (scope,)
    ).fetchall()
    for (ej,) in rows:
        try:
            for e in json.loads(ej or "[]"):
                if isinstance(e, str) and 2 <= len(e) <= 6 and RE.fullmatch(r"[\u4e00-\u9fff]+", e):
                    c[e] += 1
        except Exception:
            pass
    names = [n for n, f in c.items() if f >= min_freq]
    names.sort(key=len, reverse=True)
    return names


def _query_names(query: str, names: list[str]) -> list[str]:
    q = query
    chosen: list[str] = []
    for n in names:
        if n in q and not any(n in c for c in chosen):
            chosen.append(n)
    return chosen


def _name_channel(store: SQLiteMemoryStore, scope: str, names: list[str], limit: int):
    """内容-专名 LIKE 通道原型：正文含查询专名 → 候选；命中名字数排序。"""
    hits: dict[str, int] = {}
    ts: dict[str, int] = {}
    for n in names:
        rows = store.connection.execute(
            "SELECT id, event_ts_ms FROM memories WHERE scope=? AND status='ACTIVE' AND content LIKE ?",
            (scope, f"%{n}%"),
        ).fetchall()
        for rid, tms in rows:
            hits[rid] = hits.get(rid, 0) + 1
            ts.setdefault(rid, tms)
    ranked = sorted(hits.items(), key=lambda kv: (-kv[1], -(ts.get(kv[0]) or 0)))
    return ranked[:limit]


def run_variant(db_path: Path, scope: str, mode: str, top_k: int = 3) -> list[dict]:
    store = SQLiteMemoryStore(db_path=str(db_path), embedding_dim=512)
    settings = Settings(
        data_dir=db_path.parent, embedding_provider="hash", scene="novel",
        entity_channel=(mode == "B"), chapter_channel=True,
        rare_enabled=False, journal_mode="off", _env_file=None,
    )
    llm = LLMClient(settings)
    names = _cjk_names(store, scope) if mode == "C" else []
    try:
        return _run_probes(store, llm, settings, names, mode, top_k, scope)
    finally:
        store.close()


def _run_probes(store, llm, settings, names, mode, top_k, scope):
    out = []
    for query, expect in CHECK_PROBES:
        t0 = time.perf_counter()
        if mode == "C":
            qn = _query_names(query, names)
            if not qn:
                cands: list = []
            else:
                cands = _name_channel(store, scope, qn, top_k)
            # 合并主通道（FTS+向量）排名以贴近真实流水
            base = search_memory(store, llm, settings, query=query, scope=scope, top_k=top_k)
            fused: dict[str, float] = {}
            for i, (rid, cnt) in enumerate(cands, start=1):
                fused[rid] = fused.get(rid, 0.0) + 1.0 / (60 + i) * 2.0  # 实体命中加成
            final = []
            for r in base:
                final.append((r, fused.get(r.id, 0.0)))
            final.sort(key=lambda p: p[1], reverse=True)
            contents = [r.content for r, _ in final[:top_k]]
        else:
            results = search_memory(store, llm, settings, query=query, scope=scope, top_k=top_k)
            contents = [r.content for r in results]
        ms = (time.perf_counter() - t0) * 1000
        hit = any(expect in c for c in contents)
        out.append({"query": query, "expect": expect, "hit": hit, "ms": round(ms, 1)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("book_root")
    ap.add_argument("-o", "--out", default="")
    a = ap.parse_args()
    root = Path(a.book_root).resolve()
    db = root / ".cortexa" / "cortexa.db"
    scope = root.name
    assert db.is_file(), db
    report = {"scope": scope, "probes": len(CHECK_PROBES), "variants": {}}
    for mode in ("A_off", "B_entity", "C_content_name"):
        with tempfile.TemporaryDirectory(prefix="cortexa-ab-") as td:
            copy = Path(td) / "cortexa.db"
            shutil.copy2(db, copy)
            rows = run_variant(copy, scope, mode[0])
        hits = sum(1 for r in rows if r["hit"])
        avg = sum(r["ms"] for r in rows) / len(rows)
        report["variants"][mode] = {"hits": hits, "total": len(rows), "avg_ms": round(avg, 1), "rows": rows}
        print(f"[{mode}] hits {hits}/{len(rows)}  avg {avg:.1f} ms")
        for r in rows:
            print(f"    {'✓' if r['hit'] else '✗'} {r['query']} <- {r['expect']} ({r['ms']}ms)")
    if a.out:
        import json
        Path(a.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("report:", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())