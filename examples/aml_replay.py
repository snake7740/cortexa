"""A2 本地回放器 v0（2026-09-06）：AML 官方规模的本地近似 + 逐题失败分桶。

背景：官方不公开题目/答案（见 docs/research/2026-09-06-AML赛事素材全网检索.md），
本地只能用「上游数据集 x 我们六条官方 pipeline 契约镜像」近似，结果一律标注
『本地代理结果（非官方成绩）』。本工具做检索侧评测与逐题分桶（fusion/channel/
rerank 三分法，rerank 桶因无重排阶段恒为 0——免疫项，占位保留以便未来对齐）。

数据格式（JSON Lines；每行一个会话）：
    {"conversation_id": "conv-0", "scope_override": null,
     "messages": [{"role": "user", "content": "...", "timestamp_ms": 1700000000000}],
     "questions": [{"qid": "q0", "query": "...", "evidence_texts": ["..."], "gold": "..."}]}
    evidence_texts = 支撑答案的记忆原文（精确内容匹配定位记忆 id，与 local_eval 同法）
    gold 仅存档（判答/判官接入属 v1，需官方 judge 口径 + gpt-4o-mini 顶替并明示）

分桶口径（检索侧）：
    hit          证据在最终 top_k 内
    fusion-miss  证据被至少一个通道召回（原始通道集内）但未进最终 top_k
    channel-miss 所有词法/向量/章节/实体通道都没召回该证据
    not-in-corpus 证据文本不在库中（数据标注错误或跨会话引用）
rerank 桶不存在（确定性排序，无重排阶段）——保留字段以对齐上届三分法。

用法：
    python examples/aml_replay.py --data convs.jsonl --out cortexa-out
    python examples/aml_replay.py --selfcheck   # 内置微型集自检（无数据也可跑）

A3 成对基线（hash vs 本地 MiniLM，agentmemory 式 BM25+FTS 对照）：
    # 先装依赖 + 预下载模型（venv）：
    #   cortexa-mvp/.venv/Scripts/python.exe -m pip install sentence-transformers
    #   cortexa-mvp/.venv/Scripts/python.exe -c "from sentence_transformers import
    #     SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
    #   （HF 下载失败时设 HF_ENDPOINT=https://hf-mirror.com 再跑）
    python examples/aml_replay.py --data convs.jsonl --provider hash   --embedding-dim 512 --label hash
    python examples/aml_replay.py --data convs.jsonl --provider minilm --embedding-dim 384 --label minilm
    同一份数据两个 label 的报告并排比 buckets/hit@K 即 A3 成对结果（本地代理口径）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 自足：不依赖 cwd/PYTHONPATH

from cortexa.config import Settings  # noqa: E402
from cortexa.journal import TaskJournal  # noqa: E402
from cortexa.llm import LLMClient  # noqa: E402
from cortexa.models import AddRequest, AmlMessage, content_hash  # noqa: E402
from cortexa.modules.pipeline import run_add  # noqa: E402
from cortexa.modules.search import extract_chapter_nos, search_memory  # noqa: E402
from cortexa.storage import SQLiteMemoryStore  # noqa: E402

CHANNELS = ("fts", "vec", "chapter", "entity")
BUCKETS = ("hit", "fusion-miss", "channel-miss", "not-in-corpus")

DISCLAIMER = "本地代理结果（非官方成绩）：上游数据 x 官方契约镜像近似，检索侧分桶；判官未接入。"

SELFCHECK_CONVS = [
    {
        "conversation_id": "selfcheck-1",
        "messages": [
            {"role": "user", "content": "Alice moved to Sweden in 2019.", "timestamp_ms": 1700000000000},
            {"role": "user", "content": "Alice adopted a dog named Rob.", "timestamp_ms": 1700000001000},
        ],
        "questions": [
            {"qid": "s1-q1", "query": "Where does Alice live now?",
             "evidence_texts": ["Alice moved to Sweden in 2019."], "gold": ""},
            {"qid": "s1-q2", "query": "When did the database migration finish?",
             "evidence_texts": ["The database migration finished on March 3rd."], "gold": ""},
        ],
    }
]


def classify_evidence(ev_id: str | None, final_ids: list[str],
                      raw_by_channel: dict[str, set[str]]) -> tuple[str, list[str]]:
    """单条证据分桶（纯函数，测试友好）。返回 (bucket, 召回该证据的通道列表)。"""
    if ev_id is None:
        return "not-in-corpus", []
    if ev_id in final_ids:
        return "hit", [c for c, ids in raw_by_channel.items() if ev_id in ids]
    ch = [c for c, ids in raw_by_channel.items() if ev_id in ids]
    return ("fusion-miss" if ch else "channel-miss"), ch


def _evidence_id(store: SQLiteMemoryStore, scope: str, text: str) -> str | None:
    mem = store.get_by_hash(scope, content_hash(scope, text))
    return mem.id if mem else None


def channel_recall(store: SQLiteMemoryStore, llm: LLMClient, settings: Settings,
                   query: str, scope: str, limit: int = 100) -> dict[str, set[str]]:
    """原始通道召回（诊断用）：fts/vec/chapter/entity 各自独立取 id 集。

    与 search_memory 的正式融合不同：这里不融合不截断，只回答『证据有没有被
    任何单通道找到』——fusion/channel miss 分桶的依据。
    """
    out: dict[str, set[str]] = {}
    out["fts"] = {m.id for m, _ in store.fts_search(query, scope, limit)}
    out["vec"] = {
        m.id for m, _ in store.vector_search(
            llm.embed([query])[0], scope, limit,
            min_cos=settings.vector_min_cos_hash,
        )
    }
    chapter_nos = extract_chapter_nos(query)
    out["chapter"] = (
        {m.id for m, _ in store.chapter_search(chapter_nos, scope, limit)}
        if chapter_nos else set()
    )
    out["entity"] = {m.id for m, _ in store.entity_search(query, scope, limit)}
    return out


def run_replay_convs(convs: list[dict], out_dir: Path, top_k: int = 5,
                      provider: str = "hash", embedding_dim: int = 512,
                      label: str = "") -> dict:
    """对若干会话执行 Add -> 逐题 Search + 分桶。返回结构化报告。

    provider/embedding_dim 决定向量通道（A3 成对基线：hash@512 vs minilm@384）。
    """
    questions_meta: list[dict] = []
    bucket_counter: Counter = Counter()
    hit1 = hit5 = total_q = 0
    label = label or provider

    with tempfile.TemporaryDirectory(prefix="cortexa-replay-") as td:
        settings = Settings(
            data_dir=Path(td) / "d", embedding_provider=provider,
            embedding_dim=embedding_dim, scene="aml", journal_mode="off",
            _env_file=None,
        )
        settings.data_dir.mkdir(parents=True, exist_ok=True)  # 直连 store 需先建目录（server 路径由 create_app 代建）
        store = SQLiteMemoryStore(str(settings.data_dir / "cortexa.db"),
                                  embedding_dim=settings.embedding_dim)
        llm = LLMClient(settings)
        journal = TaskJournal(store.connection, mode="off")
        try:
            for conv in convs:
                scope = str(conv.get("conversation_id") or conv.get("scope_override") or "conv")
                messages = conv.get("messages") or []
                if messages:
                    req = AddRequest(
                        request_id=str(conv.get("conversation_id") or scope),
                        messages=[
                            AmlMessage(role=m.get("role", "user"), content=m["content"],
                                       timestamp=m.get("timestamp_ms"))
                            for m in messages
                        ],
                        scope=scope,
                        session_id=scope,
                    )
                    run_add(store, llm, settings, req, journal=journal)

                for q in conv.get("questions") or []:
                    total_q += 1
                    query = q["query"]
                    t0 = time.perf_counter()
                    final = search_memory(store, llm, settings, query=query,
                                          scope=scope, top_k=top_k, journal=None)
                    search_ms = round((time.perf_counter() - t0) * 1000, 1)
                    final_ids = [r.id for r in final]
                    raw = channel_recall(store, llm, settings, query, scope)
                    ev_rows = []
                    for text in q.get("evidence_texts") or []:
                        ev_id = _evidence_id(store, scope, text)
                        bucket, ch = classify_evidence(ev_id, final_ids, raw)
                        bucket_counter[bucket] += 1
                        rank = (final_ids.index(ev_id) + 1
                                if ev_id is not None and ev_id in final_ids else None)
                        if bucket == "hit":
                            if rank == 1:
                                hit1 += 1
                            if rank is not None and rank <= 5:
                                hit5 += 1
                        ev_rows.append({
                            "evidence": text[:80], "bucket": bucket,
                            "rank": rank, "channels": sorted(ch),
                        })
                    questions_meta.append({
                        "qid": q.get("qid", ""), "conv": scope, "query": query[:120],
                        "top_k": top_k, "search_ms": search_ms,
                        "evidence": ev_rows,
                    })
        finally:
            store.close()

    report = {
        "disclaimer": DISCLAIMER,
        "meta": {
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "上游数据集近似（文件来源见调用参数）", "top_k": top_k,
            "provider": provider, "embedding_dim": embedding_dim, "label": label,
            "conversations": len(convs), "questions": total_q,
        },
        "totals": {
            "questions": total_q,
            "buckets": dict(bucket_counter),
            "evidence_hit@1": hit1, "evidence_hit@5": hit5,
        },
        "questions": questions_meta,
    }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        out_dir.joinpath(f"aml_replay_{ts}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        md = _to_md(report)
        out_dir.joinpath(f"aml_replay_{ts}.md").write_text(md, encoding="utf-8")
        report["out_dir"] = str(out_dir)
        print(md)
    return report


def _to_md(report: dict) -> str:
    t = report["totals"]
    lines = [
        "# AML 本地回放（A2 v0）",
        "",
        "> " + report["disclaimer"],
        "",
        "- 会话数：" + str(report["meta"]["conversations"]),
        "- 题数：" + str(t["questions"]) + "  top_k=" + str(report["meta"]["top_k"]),
        "- 证据分桶：" + json.dumps(t["buckets"], ensure_ascii=False),
        "- 证据级 hit@1=" + str(t["evidence_hit@1"]) + "  hit@5=" + str(t["evidence_hit@5"]),
        "",
        "| qid | conv | bucket | rank | channels | query |",
        "|---|---|---|---|---|---|",
    ]
    for q in report["questions"]:
        for e in q["evidence"]:
            lines.append(
                f"| {q['qid']} | {q['conv']} | {e['bucket']} | {e['rank']} | "
                f"{','.join(e['channels'])} | {q['query']} |"
            )
    return "\n".join(lines)


def load_convs(path: Path) -> list[dict]:
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else [data]
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="AML 本地回放器 v0（检索侧分桶）")
    ap.add_argument("--data", type=Path, help="会话 JSON/JSONL 路径")
    ap.add_argument("--out", type=Path, default=ROOT / "cortexa-out",
                    help="报告输出目录（默认 cortexa-out）")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--provider", default="hash",
                    help="向量提供者：hash（默认）| minilm（A3 对照，需 384 维+模型）")
    ap.add_argument("--embedding-dim", type=int, default=512,
                    help="向量维度：hash=512（默认）；minilm=384")
    ap.add_argument("--label", default="",
                    help="报告 label（成对实验区分档位；默认取 provider 名）")
    ap.add_argument("--selfcheck", action="store_true", help="跑内置微型集（无数据可演示）")
    args = ap.parse_args()

    if args.selfcheck:
        convs = SELFCHECK_CONVS
    elif args.data is not None:
        convs = load_convs(args.data)
    else:
        ap.error("需要 --data 或 --selfcheck")
    run_replay_convs(convs, out_dir=args.out, top_k=args.top_k,
                     provider=args.provider, embedding_dim=args.embedding_dim,
                     label=args.label)


if __name__ == "__main__":
    main()
