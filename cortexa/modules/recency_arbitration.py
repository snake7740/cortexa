"""recency-arbitrated retrieval（2026-09-06，审计后唯一立项轴）。

读时仲裁：同一 subject 的冲突簇(簇内存在两两共享实义词/非主体实体对)内，
按事件时间显式重排为最新在前——"当前有效"占簇首，旧态保留作支撑。
与治理 v2 启发同源；默认关(recency_arbitration_enabled)；确定性、不改召回集。
"""
from __future__ import annotations

import re

from cortexa.models import MemoryInstance

_STOP = frozenset({
    "the", "this", "that", "these", "those", "and", "for", "with", "from",
    "was", "were", "been", "have", "has", "had", "will", "would", "shall",
    "should", "can", "could", "may", "might", "must", "not", "but", "its",
    "our", "your", "their", "into", "over", "under", "then", "than", "also",
})


def subject_of(mem: MemoryInstance) -> str | None:
    if mem.entities:
        return mem.entities[0].lower()
    if mem.topic:
        return mem.topic.lower()
    return None


def _terms(mem: MemoryInstance) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", (mem.content or "").lower())
            if len(w) >= 5 and w not in _STOP}


def _attr(mem: MemoryInstance) -> set[str]:
    s = subject_of(mem)
    return {e.lower() for e in (mem.entities or []) if e.lower() != s}


def _ts(mem: MemoryInstance) -> int:
    return (mem.event_ts_ms if mem.event_ts_ms is not None
            else mem.created_ts_ms or 0)


def _conflict(a: MemoryInstance, b: MemoryInstance) -> bool:
    return bool(_terms(a) & _terms(b)) or bool(_attr(a) & _attr(b))


def arbitrate(scored: list, top_n: int = 50) -> list:
    """簇内按事件时间显式重排(最新在前)；无冲突簇不动；返回新列表。"""
    out = list(scored)
    clusters: dict[str, list[int]] = {}
    for i, (mem, _s) in enumerate(out[:top_n]):
        s = subject_of(mem)
        if s:
            clusters.setdefault(s, []).append(i)
    for idxs in clusters.values():
        if len(idxs) < 2:
            continue
        mems = [out[i][0] for i in idxs]
        if not any(_conflict(mems[a], mems[b])
                   for a in range(len(mems)) for b in range(a + 1, len(mems))):
            continue
        if len({_ts(m) for m in mems}) < 2:
            continue
        ordered = sorted(
            [(out[i], i) for i in idxs],
            key=lambda pair: (_ts(pair[0][0]), pair[1]),
            reverse=True,
        )
        for pos, (pair, _orig_i) in zip(sorted(idxs), ordered):
            out[pos] = pair
    return out
