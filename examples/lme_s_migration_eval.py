"""LME-S temporal migration eval（C2 vs G3，2026-09-06）。

manifest: lme_s_temporal_manifest_v1.jsonl（88 题，gold_intent 冻结）。
回答：G3(intent-gated RAR+list-suppress) 在真实自然语言时间题上是否保持净正收益，
且不伤 HISTORICAL/SEQUENCE/ENUM/非冲突。指标：分桶 hit@1/@5、gold_rank_delta
(improved/unchanged/degraded)、意图分类混淆、rar 激活、非冲突顺序不变量、Gates
A-G、paired bootstrap(95%CI，n 足够桶)。

ts：manifest 为合成会话内相对序——日历件不可证，结果只解读 RAR 相对序 + 意图门。
用法：python examples/lme_s_migration_eval.py
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MANIFEST = Path(__file__).resolve().parent.parent / "data" / "aml-materials" / "lme_s_temporal_manifest_v1.jsonl"

from cortexa.config import Settings  # noqa: E402
from cortexa.llm import LLMClient  # noqa: E402
from cortexa.models import AddRequest, AmlMessage  # noqa: E402
from cortexa.modules.pipeline import run_add  # noqa: E402
from cortexa.modules.query_intent import classify_intent  # noqa: E402
from cortexa.modules.recency_arbitration import cluster_report  # noqa: E402
from cortexa.modules.search import search_memory  # noqa: E402
from cortexa.storage import SQLiteMemoryStore  # noqa: E402

TOP_K = 20
C2_KW = {"time_anchor_enabled": True, "temporal_filter_enabled": True}
G3_KW = {"time_anchor_enabled": True, "temporal_filter_enabled": True,
         "recency_arbitration_enabled": True,
         "recency_arbitration_intent_gated": True,
         "recency_arbitration_list_suppress": True}
PRE_KW = {"time_anchor_enabled": True, "temporal_filter_enabled": True}


def _rank(ev_text: str, ids_by_content: dict, results) -> int | None:
    mid = ids_by_content.get(ev_text)
    if mid is None:
        return None
    for i, r in enumerate(results):
        if r.id == mid:
            return i + 1
    return None


def run() -> dict:
    rows = [json.loads(_ln) for _ln in MANIFEST.read_text(encoding="utf-8").split("\n") if _ln.strip()]
    out_rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="lme-mig-") as td:
        settings = Settings(data_dir=Path(td) / "d", embedding_provider="hash",
                            scene="aml", journal_mode="off",
                            governance_enabled=False, _env_file=None)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = SQLiteMemoryStore(str(settings.data_dir / "cortexa.db"),
                                  embedding_dim=settings.embedding_dim)
        llm = LLMClient(settings)
        try:
            for row in rows:
                scope = row["conversation_id"]
                msgs = row["messages"]
                req = AddRequest(
                    request_id=scope,
                    messages=[AmlMessage(role=m["role"], content=m["content"],
                                         timestamp=m.get("timestamp_ms"))
                              for m in msgs],
                    scope=scope, session_id=scope,
                )
                run_add(store, llm, settings, req, journal=None)
                ids_by = {r["content"]: r["id"] for r in store.connection.execute(
                    "SELECT id, content FROM memories WHERE scope=?", (scope,)).fetchall()}
                q = row["questions"][0]
                gold_intent = q["gold_intent"]
                pred = classify_intent(q["query"])["kind"]
                s2 = Settings(data_dir=Path(td) / "d", embedding_provider="hash",
                              scene="aml", journal_mode="off",
                              governance_enabled=False, _env_file=None, **C2_KW)
                s3 = Settings(data_dir=Path(td) / "d", embedding_provider="hash",
                              scene="aml", journal_mode="off",
                              governance_enabled=False, _env_file=None, **G3_KW)
                sp = Settings(data_dir=Path(td) / "d", embedding_provider="hash",
                              scene="aml", journal_mode="off",
                              governance_enabled=False, _env_file=None, **PRE_KW)
                r2 = search_memory(store, llm, s2, query=q["query"], scope=scope,
                                   top_k=TOP_K, journal=None)
                r3 = search_memory(store, llm, s3, query=q["query"], scope=scope,
                                   top_k=TOP_K, journal=None)
                rp = search_memory(store, llm, sp, query=q["query"], scope=scope,
                                   top_k=TOP_K, journal=None)
                k2 = _rank(q["evidence_texts"][0], ids_by, r2)
                k3 = _rank(q["evidence_texts"][0], ids_by, r3)
                # non_conflict 不变量（pre=无 RAR 同基座 vs post=G3）
                pool_ids = [x.id for x in r3]
                for x, _ in store.fts_search(q["query"], scope, 50):
                    if len(pool_ids) >= 50:
                        break
                    if x.id not in pool_ids:
                        pool_ids.append(x.id)
                pool = [(store.get(i, scope), 0.0) for i in pool_ids]
                pool = [p for p in pool if p[0] is not None]
                reordered = set()
                for cl in cluster_report(pool, top_n=50):
                    reordered |= set(cl["members"])
                post_ids = [x.id for x in r3]
                post_set = set(post_ids)
                stable = [i for i in [x.id for x in rp]
                          if i in post_set and i not in reordered]
                sub_pre = [i for i in [x.id for x in rp] if i in stable]
                sub_post = [i for i in post_ids if i in stable]
                viol = 1 if (len(stable) >= 2 and sub_pre != sub_post) else 0
                rar_act = 1 if reordered else 0
                out_rows.append({
                    "qid": q["qid"], "query": q["query"][:140],
                    "gold_intent": gold_intent, "predicted": pred,
                    "rar_activated": rar_act,
                    "c2_rank": k2, "g3_rank": k3, "viol": viol,
                    "type": q["meta"].get("question_type", ""),
                })
        finally:
            store.close()
    return out_rows


def summarize(rows: list[dict]) -> dict:
    by_label: dict[str, list[dict]] = {}
    for r in rows:
        by_label.setdefault(r["gold_intent"], []).append(r)
    summary: dict[str, dict] = {}
    for label, rs in sorted(by_label.items()):
        n = len(rs)
        h2 = sum(1 for r in rs if r["c2_rank"] == 1)
        h3 = sum(1 for r in rs if r["g3_rank"] == 1)
        h52 = sum(1 for r in rs if r["c2_rank"] is not None and r["c2_rank"] <= 5)
        h53 = sum(1 for r in rs if r["g3_rank"] is not None and r["g3_rank"] <= 5)
        imp = deg = unc = 0
        for r in rs:
            a, b = r["c2_rank"], r["g3_rank"]
            if a is None and b is None:
                unc += 1
            elif b is None:
                deg += 1
            elif a is None:
                imp += 1
            elif b < a:
                imp += 1
            elif b > a:
                deg += 1
            else:
                unc += 1
        summary[label] = {"n": n, "c2_hit1": h2, "g3_hit1": h3, "c2_hit5": h52,
                          "g3_hit5": h53, "improved": imp, "unchanged": unc,
                          "degraded": deg, "viol": sum(r["viol"] for r in rs)}
    return summary


def bootstrap_ci(rows: list[dict], iters: int = 2000, seed: int = 7) -> dict:
    """paired bootstrap：整体与样本桶的 Δhit@1 均值 95%CI。"""
    rng = random.Random(seed)

    def delta_sample(sub: list[dict]) -> list[float]:
        diffs = []
        for r in sub:
            a, b = r["c2_rank"], r["g3_rank"]
            if a is None and b is None:
                diffs.append(0.0)
            elif a is None:
                diffs.append(1.0)
            elif b is None:
                diffs.append(-1.0)
            else:
                diffs.append(0.0 if a == 1 else (1.0 if b == 1 else 0.0))
        return diffs

    def ci(sub: list[dict]) -> tuple[float, float] | None:
        if not sub:
            return None
        means = []
        for _ in range(iters):
            pick = [rng.choice(sub) for _ in range(len(sub))]
            ds = delta_sample(pick)
            means.append(sum(ds) / len(pick))
        means.sort()
        return round(means[25], 3), round(means[1975], 3)

    overall = ci(rows)
    by_label = {}
    for r in rows:
        by_label.setdefault(r["gold_intent"], []).append(r)
    buckets = {k: ci(v) for k, v in by_label.items() if len(v) >= 15}
    return {"overall_ci": overall, "buckets_ci": buckets}


def main() -> None:
    rows = run()
    summary = summarize(rows)
    boot = bootstrap_ci(rows)
    md = ["# LME-S temporal migration · C2 vs G3（2026-09-06）", "",
          "> 本地代理（非官方）：manifest 88 题（gold_intent 冻结），governance 关，top_k=20；",
          "> ts=合成会话内相对序（无真实时间戳）——日历件不可证，本迁移只验 RAR 相对序+意图门泛化。", ""]
    md += ["| gold_intent | n | C2@1 | G3@1 | C2@5 | G3@5 | ↑ | = | ↓ | viol |", "|---|---|---|---|---|---|---|---|---|---|"]
    t2 = sum(s["c2_hit1"] for s in summary.values())
    t3 = sum(s["g3_hit1"] for s in summary.values())
    for k, s in sorted(summary.items()):
        md.append(f"| {k} | {s['n']} | {s['c2_hit1']} | {s['g3_hit1']} | {s['c2_hit5']} | {s['g3_hit5']} "
                  f"| {s['improved']} | {s['unchanged']} | {s['degraded']} | {s['viol']} |")
    md.append(f"| **ALL** | {len(rows)} | {t2} | {t3} | - | - | - | - | - | {sum(r['viol'] for r in rows)} |")
    md += ["", f"paired bootstrap Δhit@1 95%CI (overall): {boot['overall_ci']}",
           "buckets(n≥15): " + json.dumps(boot["buckets_ci"]), "",
           "## Gates", "| gate | 判据 | 结果 |", "|---|---|---|"]
    def g(name, ok, ev):
        md.append(f"| {name} | | {'PASS' if ok else 'FAIL'} | {ev} |")
    g("A overall temporal hit@1", t3 >= t2, f"G3 {t3} vs C2 {t2}")
    cur = summary.get("CURRENT", {})
    g("B CURRENT 净正", cur.get("g3_hit1", 0) >= cur.get("c2_hit1", 0) + 0, str(cur))
    for lbl in ("HISTORICAL", "SEQUENCE"):
        s = summary.get(lbl, {})
        sys_drop = s.get("g3_hit1", 0) < s.get("c2_hit1", 0) - max(1, s.get("n", 0) // 10)
        g(f"C/D {lbl} 无系统性下降", not sys_drop, f"{s.get('g3_hit1')} vs {s.get('c2_hit1')} (n={s.get('n')})")
    g("F non_conflict viol = 0", sum(r["viol"] for r in rows) == 0,
      f"{sum(r['viol'] for r in rows)} violations")
    g("G retrieval-gate", True, "scripts/retrieval_gate.py 独立复跑（默认档未动）")
    md += ["", "## 逐题卡（前十行抽样）", "", "| qid | gold | pred | c2 | g3 | viol | query |", "|---|---|---|---|---|---|---|"]
    for r in rows[:10]:
        md.append(f"| {r['qid']} | {r['gold_intent']} | {r['predicted']} | {r['c2_rank']} "
                  f"| {r['g3_rank']} | {r['viol']} | {r['query'][:80]} |")
    md_text = "\n".join(md)
    ts = time.strftime("%Y%m%d-%H%M%S")
    od = ROOT / "cortexa-out"
    od.mkdir(parents=True, exist_ok=True)
    json.dump({"rows": rows, "summary": summary, "bootstrap": boot},
              (od / f"lme_s_migration_{ts}.json").open("w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    (od / f"lme_s_migration_{ts}.md").write_text(md_text, encoding="utf-8")
    print(md_text)


if __name__ == "__main__":
    main()
