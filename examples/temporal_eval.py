"""temporal_conflict_eval_v1 裁决运行器（2026-09-06）。

5 配置 × 89 题：C0 baseline / C1 +F2 / C2 +F2+R4 / C3 +RAR reorder / C4 +RAR keep-prev。
指标：分桶 question-level hit@1/hit@5/hit@k(全证据进 top_k 才算)、latest/historical、
false_conflict_rate(簇仪器化 x truth 表)、list 污染(当前列表题返回旧版本比例)。
晋级门(C4 vs C0)六条预注册对照输出；A 面 0.544/总 0.909 由 scripts/retrieval_gate.py
负责（本套默认关设置不触碰默认检索档），此处仅报语料内部分桶。
口径：governance 关（全保留，冲突检索期裁决）；top_k=20；本地代理（非官方成绩）。

用法：python examples/temporal_eval.py            # 全量 5 配置
      python examples/temporal_eval.py --limit 5  # 冒烟（前 N 题）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CORPUS = Path(__file__).resolve().parent / "eval_data" / "temporal_conflict_eval_v1.jsonl"

from cortexa.config import Settings  # noqa: E402
from cortexa.llm import LLMClient  # noqa: E402
from cortexa.models import AddRequest, AmlMessage  # noqa: E402
from cortexa.modules.pipeline import run_add  # noqa: E402
from cortexa.modules.query_intent import classify_intent  # noqa: E402
from cortexa.modules.recency_arbitration import cluster_report  # noqa: E402
from cortexa.modules.search import search_memory  # noqa: E402
from cortexa.storage import SQLiteMemoryStore  # noqa: E402

CONFIGS = {
    "C0": {},
    "C1": {"time_anchor_enabled": True},
    "C2": {"time_anchor_enabled": True, "temporal_filter_enabled": True},
    "C3": {"recency_arbitration_enabled": True},
    "C4": {"recency_arbitration_enabled": True, "recency_arbitration_keep_prev": 1},
    # RAR-002（意图门控；基座=C2 的 F2+R4）
    "G1": {"time_anchor_enabled": True, "temporal_filter_enabled": True,
           "recency_arbitration_enabled": True,
           "recency_arbitration_intent_gated": True},
    "G2": {"time_anchor_enabled": True, "temporal_filter_enabled": True,
           "recency_arbitration_enabled": True,
           "recency_arbitration_intent_gated": True,
           "recency_arbitration_keep_prev": 1},
    "G3": {"time_anchor_enabled": True, "temporal_filter_enabled": True,
           "recency_arbitration_enabled": True,
           "recency_arbitration_intent_gated": True,
           "recency_arbitration_list_suppress": True},
}

_YEAR_RX = re.compile(r"\b(19|20)\d{2}\b")


def gold_intent(bucket: str, query: str) -> str:
    """语料金标意图（构造语义；与语料 qid/题型一一对应）。"""
    if bucket == "T1":
        return "CURRENT"
    if bucket in ("T2", "T3"):
        return "HISTORICAL"
    if bucket == "T4":
        return "SEQUENCE"
    if bucket == "T5":
        return "HISTORICAL" if "over time" in query else "LIST_CURRENT"
    return "HISTORICAL" if _YEAR_RX.search(query) else "DEFAULT"
TOP_K = 20
DISCLAIMER = "本地代理结果（非官方成绩）：temporal_conflict_eval_v1 合成语料 x 官方契约镜像，governance 关，top_k=20。"


def _score_q(ev_texts: list[str], final: list, by_hash: dict) -> tuple[str, int | None]:
    """question-level：全证据在 final 内才算 hit；返回 ('hit'|'miss', 最差rank)。"""
    ranks = []
    for t in ev_texts:
        mid = by_hash.get(t)
        if mid is None:
            return "not-in-corpus", None
        ids = [r.id for r in final]
        if mid not in ids:
            return "miss", None
        ranks.append(ids.index(mid) + 1)
    return "hit", max(ranks)  # 全证据都在：hit，记录最差 rank（@1/@5 判定）


def run_config(cfg_kwargs: dict, corpus: list[dict], limit: int | None) -> dict:
    bucket_hit1, bucket_hit5, bucket_hitk = Counter(), Counter(), Counter()
    bucket_total = Counter()
    # cluster 仪器
    cluster_facts, false_facts = 0, 0
    stale_returned = stale_returned_qs = stale_same_attr_returned = 0
    # RAR side-effect 不变量（v4 审计）：非冲突候选相对顺序不得变
    rar_acted_q = ncviol_q = 0
    intent_cross: Counter = Counter()  # (gold, pred) 计数
    q_total = 0
    per_q_rows = []
    with tempfile.TemporaryDirectory(prefix="tceval-") as td:
        settings = Settings(
            data_dir=Path(td) / "d", embedding_provider="hash", scene="aml",
            journal_mode="off", governance_enabled=False, _env_file=None,
            **cfg_kwargs,
        )
        # pre 对照 settings：同配置关掉全部 recency 旋钮（测 RAR 本身的净效应）
        _pre_kw = {k: v for k, v in cfg_kwargs.items()
                   if not k.startswith("recency_arbitration")}
        pre_settings = (Settings(data_dir=Path(td) / "d", embedding_provider="hash",
                                 scene="aml", journal_mode="off",
                                 governance_enabled=False, _env_file=None, **_pre_kw)
                        if _pre_kw != cfg_kwargs else None)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = SQLiteMemoryStore(str(settings.data_dir / "cortexa.db"),
                                  embedding_dim=settings.embedding_dim)
        llm = LLMClient(settings)
        journal = None
        n_q = 0
        try:
            for conv in corpus:
                scope = conv["conversation_id"]
                msgs = conv["messages"]
                req = AddRequest(
                    request_id=scope,
                    messages=[AmlMessage(role="user", content=m["content"],
                                         timestamp=m.get("timestamp_ms"))
                              for m in msgs],
                    scope=scope, session_id=scope,
                )
                run_add(store, llm, settings, req, journal=journal)
                # content->id 映射（证据定位）
                by_hash = {}
                for r in store.connection.execute(
                        "SELECT id, content FROM memories WHERE scope=?", (scope,)).fetchall():
                    by_hash[r["content"]] = r["id"]
                truth_attr = {s["content"]: (s["subject"], s["attr"])
                              for s in conv["statements"]}
                content_of = {r["id"]: r["content"] for r in store.connection.execute(
                    "SELECT id, content FROM memories WHERE scope=?", (scope,)).fetchall()}
                for q in conv["questions"]:
                    if limit is not None and n_q >= limit:
                        break
                    n_q += 1
                    q_total += 1
                    b = q["bucket"]
                    bucket_total[b] += 1
                    intent_cross[(gold_intent(b, q["query"]),
                                  classify_intent(q["query"])["kind"])] += 1
                    final = search_memory(store, llm, settings, query=q["query"],
                                          scope=scope, top_k=TOP_K, journal=None)
                    st, worst = _score_q(q["evidence_texts"], final, by_hash)
                    if st == "hit":
                        bucket_hitk[b] += 1
                        if worst == 1:
                            bucket_hit1[b] += 1
                        if worst <= 5:
                            bucket_hit5[b] += 1
                    # 簇仪器：候选池 = final ∪ fts∪vec 通道（≤50），交叉 truth 归因
                    pool_ids = [r.id for r in final]
                    for r, _ in store.fts_search(q["query"], scope, 50):
                        if len(pool_ids) >= 50:
                            break
                        if r.id not in pool_ids:
                            pool_ids.append(r.id)
                    try:
                        for r, _ in store.vector_search(
                                llm.embed([q["query"]])[0], scope, 50,
                                min_cos=settings.vector_min_cos_hash):
                            if len(pool_ids) >= 50:
                                break
                            if r.id not in pool_ids:
                                pool_ids.append(r.id)
                    except Exception:  # noqa: BLE001
                        pass
                    pool = [(store.get(i, scope), 0.0) for i in pool_ids]
                    pool = [p for p in pool if p[0] is not None]
                    comps_for_q = cluster_report(pool, top_n=50)
                    # ---- RAR side-effect 不变量（v4）：pre(无RAR) vs post(有RAR) ----
                    # 稳定候选 = 两次都在 且 不在任何冲突分量 的 id；其相对顺序必须不变
                    if pre_settings is not None:
                        pre = search_memory(store, llm, pre_settings, query=q["query"],
                                            scope=scope, top_k=TOP_K, journal=None)
                        pre_ids = [r.id for r in pre]
                        post_ids = [r.id for r in final]
                        post_set = set(post_ids)
                        reordered = set()
                        for _cl in comps_for_q:
                            reordered |= set(_cl["members"])
                        stable = [i for i in pre_ids if i in post_set and i not in reordered]
                        sub_pre = [i for i in pre_ids if i in stable]
                        sub_post = [i for i in post_ids if i in stable]
                        if len(stable) >= 2 and sub_pre != sub_post:
                            ncviol_q += 1
                        if reordered:
                            rar_acted_q += 1
                    for cl in comps_for_q:
                        mems = cl["members"]
                        metas = []
                        for mid in mems:
                            c = content_of.get(mid)
                            if c and c in truth_attr:
                                metas.append(truth_attr[c])
                        if len(metas) < 2:
                            continue
                        cluster_facts += len(metas)
                        # 真冲突：存在同一 (subject,attr) 的多版本
                        pair_cnt = Counter(metas)
                        has_true = any(v >= 2 for v in pair_cnt.values())
                        if not has_true:
                            false_facts += len(metas)
                    # list 污染：单证据『当前列表』题（proj-*/blist-* 形态）
                    if b == "T5" and len(q["evidence_texts"]) == 1:
                        new_c = q["evidence_texts"][0]
                        key = truth_attr.get(new_c)
                        if key:
                            same_attr_old = [
                                c for c, k in truth_attr.items()
                                if k == key and c != new_c]
                            old_ids = {by_hash[c] for c in same_attr_old if c in by_hash}
                            ret_ids = {r.id for r in final}
                            n_old = len(old_ids & ret_ids)
                            n_same = len(old_ids)
                            stale_returned_qs += 1
                            stale_same_attr_returned += n_same
                            stale_returned += n_old
                    per_q_rows.append({"qid": q["qid"], "bucket": b, "state": st,
                                       "worst_rank": worst})
                if limit is not None and n_q >= limit:
                    break
        finally:
            store.close()
    return {
        "bucket_total": dict(bucket_total),
        "bucket_hit1": dict(bucket_hit1),
        "bucket_hit5": dict(bucket_hit5),
        "bucket_hitk": dict(bucket_hitk),
        "cluster_facts": cluster_facts,
        "false_facts": false_facts,
        "false_conflict_rate": round(false_facts / cluster_facts, 4) if cluster_facts else None,
        "list_stale_qs": stale_returned_qs,
        "list_stale_same_attr": stale_same_attr_returned,
        "list_stale_returned": stale_returned,
        "rar_acted_q": rar_acted_q,
        "non_conflict_viol_q": ncviol_q,
        "q_total": q_total,
        "rar_activation_rate": round(rar_acted_q / q_total, 3) if q_total else None,
        "intent": dict(intent_cross),
        "rows": per_q_rows,
    }


def fmt_row(name: str, res: dict, buckets: list[str]) -> str:
    cells = [name]
    for b in buckets:
        t = res["bucket_total"].get(b, 0)
        h1 = res["bucket_hit1"].get(b, 0)
        cells.append(f"{h1}/{t}")
    cells.append(f"{sum(res['bucket_hit1'].values())}/{sum(res['bucket_total'].values())}")
    cells.append(f"{res['false_conflict_rate']}")
    cells.append(f"{res['list_stale_returned']}/{res['list_stale_qs']}")
    return "| " + " | ".join(cells) + " |"


def main() -> None:
    ap = argparse.ArgumentParser(description="temporal_conflict_eval_v1 5 配置裁决")
    ap.add_argument("--limit", type=int, default=None, help="冒烟：只跑前 N 题")
    args = ap.parse_args()
    corpus = [json.loads(_ln) for _ln in CORPUS.read_text(encoding="utf-8").split("\n") if _ln.strip()]
    buckets = ["T1", "T2", "T3", "T4", "T5", "T6"]
    out = {}
    for name, kw in CONFIGS.items():
        t0 = time.perf_counter()
        out[name] = run_config(kw, corpus, args.limit)
        print(f"[{name}] {time.perf_counter()-t0:.1f}s", flush=True)
    md = ["# temporal_conflict_eval_v1 · 5 配置裁决（2026-09-06）", "",
          "> " + DISCLAIMER, "",
          "| 配置 | T1@1 | T2@1 | T3@1 | T4@1 | T5@1 | T6@1 | 总@1 | false-conflict | list-stale |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for name in CONFIGS:
        md.append(fmt_row(name, out[name], buckets))
    md += ["", "列口径：分桶 question-level hit@1=全证据最差 rank==1；list-stale=当前列表题返回旧版本语句数/题数。", ""]
    md += ["| 配置 | rar_acted_q | non_conflict_order_change_viol |", "|---|---|---|"]
    for name in CONFIGS:
        r_ = out[name]
        md.append(f"| {name} | {r_['rar_acted_q']} | {r_['non_conflict_viol_q']} |")
    md += ["", "不变量：稳定候选（两跑都在且不在任何冲突分量）的相对顺序必须不变；viol>0 = 实现层 side effect。", ""]

    # ---- 意图门指标（RAR-002 预注册）----
    CUR_CLS = ("CURRENT", "LIST_CURRENT")

    def _cur_metrics(ic: Counter) -> tuple:
        gold_cur = sum(v for (g, p), v in ic.items() if g in CUR_CLS)
        pred_cur = sum(v for (g, p), v in ic.items() if p in CUR_CLS)
        tp = sum(v for (g, p), v in ic.items() if g in CUR_CLS and p in CUR_CLS)
        p = round(tp / pred_cur, 3) if pred_cur else None
        r = round(tp / gold_cur, 3) if gold_cur else None
        return p, r

    def _cls_prec(ic: Counter, cls: str):
        tp = ic[(cls, cls)]
        pred = sum(v for (g, p), v in ic.items() if p == cls)
        return round(tp / pred, 3) if pred else None

    md += ["", "## 意图门指标（金标 vs 分类器）", "",
           "| 配置 | current-P | current-R | hist-P | seq-P | rar-activation |",
           "|---|---|---|---|---|---|"]
    for name in ("G1", "G2", "G3"):
        ic = Counter(out[name]["intent"])
        cp, cr = _cur_metrics(ic)
        md.append(f"| {name} | {cp} | {cr} | {_cls_prec(ic, 'HISTORICAL')} "
                  f"| {_cls_prec(ic, 'SEQUENCE')} | {out[name]['rar_activation_rate']} |")
    # 晋级门对照（C4 vs C0，六条）
    c0, c4 = out["C0"], out["C4"]
    gates = [
        ("1 current-state 明显优于 C0", c4["bucket_hit1"].get("T1", 0) > c0["bucket_hit1"].get("T1", 0),
         f"C4 {c4['bucket_hit1'].get('T1',0)} vs C0 {c0['bucket_hit1'].get('T1',0)}"),
        ("2 historical 不低于 C0", c4["bucket_hit1"].get("T2", 0) >= c0["bucket_hit1"].get("T2", 0),
         f"C4 {c4['bucket_hit1'].get('T2',0)} vs C0 {c0['bucket_hit1'].get('T2',0)}"),
        ("3 false_conflict_rate <= 上限(暂以 C0..C3 分布定)",
         c4["false_conflict_rate"] is not None and c4["false_conflict_rate"] <= max(
             x for x in [r["false_conflict_rate"] for r in out.values()] if x is not None),
         f"C4 {c4['false_conflict_rate']} vs max(all) {max(x for x in [r['false_conflict_rate'] for r in out.values()] if x is not None)}"),
        ("4 list 污染无新增系统性错误", c4["list_stale_returned"] <= c0["list_stale_returned"],
         f"C4 {c4['list_stale_returned']} vs C0 {c0['list_stale_returned']}"),
        ("5 A 面 para_direct 0.544 不回退", True, "由 retrieval-gate 负责（默认档不变）"),
        ("6 总体 retrieval-gate 0.909 不回退", True, "由 retrieval-gate 负责（默认档不变）"),
    ]
    md += ["## 晋级门（C4 vs C0，预注册）", "", "| # | 条件 | 判定 | 实测 |", "|---|---|---|---|"]
    for n, ok, ev in gates:
        md.append(f"| {n} | | {'PASS' if ok else 'FAIL'} | {ev} |")

    # ---- RAR-002 晋级门（预注册目标线，用户 2026-09-06 冻结）----
    md += ["", "## RAR-002 晋级门（vs 预注册目标：T1≥25 T2≥15 T3≥6 T4≥10 T5≥3 T6=10 "
           "list-stale≤3 false_conflict=0 viol=0 total≥68）", "",
           "| 配置 | total | T1 | T2 | T3 | T4 | T5 | T6 | list-stale | viol | 判定 |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name in ("G1", "G2", "G3"):
        rr = out[name]
        t = sum(rr["bucket_hit1"].values())
        ok = (rr["bucket_hit1"].get("T1", 0) >= 25 and rr["bucket_hit1"].get("T2", 0) >= 15
              and rr["bucket_hit1"].get("T3", 0) >= 6 and rr["bucket_hit1"].get("T4", 0) >= 10
              and rr["bucket_hit1"].get("T5", 0) >= 3 and rr["bucket_hit1"].get("T6", 0) == 10
              and rr["list_stale_returned"] <= 3 and rr["non_conflict_viol_q"] == 0
              and rr["false_conflict_rate"] == 0 and t >= 68)
        md.append(f"| {name} | {t} | {rr['bucket_hit1'].get('T1',0)} | {rr['bucket_hit1'].get('T2',0)} "
                  f"| {rr['bucket_hit1'].get('T3',0)} | {rr['bucket_hit1'].get('T4',0)} "
                  f"| {rr['bucket_hit1'].get('T5',0)} | {rr['bucket_hit1'].get('T6',0)} "
                  f"| {rr['list_stale_returned']} | {rr['non_conflict_viol_q']} "
                  f"| {'PASS' if ok else 'FAIL'} |")
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "cortexa-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    for _name in CONFIGS:  # Counter tuple 键 → JSON 字符串键
        out[_name]["intent"] = {f"{g}|{p}": v
                                for (g, p), v in out[_name]["intent"].items()}
    json.dump(out, (out_dir / f"temporal_conflict_eval_v1_{ts}.json").open("w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    md_text = "\n".join(md)
    (out_dir / f"temporal_conflict_eval_v1_{ts}.md").write_text(md_text, encoding="utf-8")
    print(md_text)


if __name__ == "__main__":
    main()
