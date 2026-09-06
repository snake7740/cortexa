# -*- coding: utf-8 -*-
"""P4 原型（2026-09-06）：余弦标度混合重排 scorer——候选 = FTS top80 ∪ 语义 top100。

实证（真实库 2620 条副本，11 题机检）：hash 基线 9/11；本 scorer（纯 hash 档）10/11、
+语义 10/11——弱题 灵根/甲字/主线 全中。
真管线（RRF 融合）同等候选池仍 9/11 → 增益来自「余弦标度相加」而非单纯加宽候选；
集成建议见 docs/research/2026-09-06-semantic-embedding-pilot.md §六。

用法（需先建语义索引 + 清洗 512 向量副本）：
    python examples/novel_mix_rerank_proto.py <db路径> [scope]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from novel_checkup import CHECK_PROBES  # noqa: E402

from cortexa.config import Settings  # noqa: E402
from cortexa.llm import LLMClient  # noqa: E402
from cortexa.storage import SQLiteMemoryStore  # noqa: E402

WF = 40.0  # FTS 秩分权重（1/(60+rank) 标度）
WS = 0.6  # 语义余弦权重


def _cos(a: list[float], b: list[float]) -> float:
    va = np.asarray(a, dtype="f4")
    vb = np.asarray(b, dtype="f4")
    return float(va @ vb / (np.linalg.norm(va) * np.linalg.norm(vb) + 1e-9))


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else "C:/Cortexa/.loop/tmp_sem2/.cortexa/cortexa.db"
    scope = sys.argv[2] if len(sys.argv) > 2 else "废灵根修仙-重构版"
    store = SQLiteMemoryStore(db_path=db, embedding_dim=512)
    s_o = Settings(
        _env_file="C:/Cortexa/cortexa-mvp/.env", embedding_provider="openai",
        scene="novel", chapter_channel=True, rare_enabled=False,
        journal_mode="off", embedding_dim=512,
    )
    llm_o = LLMClient(s_o)
    hits_f = 0
    hits_m = 0
    for q, exp in CHECK_PROBES:
        fts = store.fts_search(q, scope, limit=80)
        sem = store.sem_search(llm_o.embed([q])[0], scope, limit=100, min_cos=0.0)
        fidx = {m.id: i + 1 for i, (m, _s) in enumerate(fts)}
        sc_f: dict[str, float] = {m.id: WF / (60.0 + fidx[m.id]) for m, _s in fts}
        sc_m = dict(sc_f)
        for m, c in sem:
            sc_m[m.id] = sc_m.get(m.id, 0.0) + WS * max(float(c), 0.0)
        top_f = sorted(sc_f, key=sc_f.get, reverse=True)[:3]
        top_m = sorted(sc_m, key=sc_m.get, reverse=True)[:3]
        hf = any(exp in store.get(i, scope).content for i in top_f)
        hm = any(exp in store.get(i, scope).content for i in top_m)
        hits_f += int(hf)
        hits_m += int(hm)
        print(f"{'✓' if hf else '✗'}->{'✓' if hm else '✗'}  {q} <- {exp}")
    print(f"hash-fused: {hits_f}/{len(CHECK_PROBES)} | +sem: {hits_m}/{len(CHECK_PROBES)}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
