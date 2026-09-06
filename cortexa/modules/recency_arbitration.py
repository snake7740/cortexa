"""recency-arbitrated retrieval（2026-09-06，审计后唯一立项轴）。

读时仲裁：同一 subject 的冲突簇内，按事件时间显式重排为最新在前——"当前有效"
占簇首，旧态保留作支撑。与治理 v2 启发同源；默认关(recency_arbitration_enabled)；
确定性、不改召回集。
冲突键 v2（2026-09-06 外部复核）：只认『属性相关』信号——共享非主体实体 / 同属性槽
(entities[1] 或 topic≠subject) / 共享非泛词内容词（排除高频谓词与主语词元）。
修复跨属性误冲突："alice works at google" 与 "alice's brother works at microsoft"
共享 works 但属性不同——不再仲裁。
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

# 高频谓词/无属性信息量词（v2，2026-09-06）：单独出现不足以证明两记忆谈同一属性
# （治理 _GENERIC_ATTR 同源思路；"works" 案例=Alice 雇主 vs 其兄弟雇主）。
_GENERIC = frozenset({
    "works", "worked", "working", "lives", "lived", "living", "moved",
    "moving", "moves", "likes", "liked", "loves", "loved", "bought", "buys",
    "wants", "wanted", "went", "goes", "going", "came", "told", "said",
    "made", "makes", "got", "getting", "started", "starting", "starts",
    "joined", "attended", "visiting", "visited", "planned", "planning",
    "thinks", "thought", "feels", "felt", "seems", "hoping", "hoped",
    "looking", "asked", "asking", "trying", "tried", "helps", "helped",
    "using", "used", "uses", "talks", "talked", "talking", "enjoys",
    "prefers", "preferred", "decided", "deciding", "considered", "mentioned",
})


def subject_of(mem: MemoryInstance) -> str | None:
    if mem.entities:
        return mem.entities[0].lower()
    if mem.topic:
        return mem.topic.lower()
    return None


def _terms(mem: MemoryInstance) -> set[str]:
    """属性相关内容词：≥5 字符、非停用/非泛词、非主语词元（主语共享不算冲突证据）。"""
    s = subject_of(mem)
    subj_toks = set((s or "").split())
    return {w for w in re.findall(r"[a-z0-9']+", (mem.content or "").lower())
            if len(w) >= 5 and w not in _STOP and w not in _GENERIC
            and w not in subj_toks}


def _attr(mem: MemoryInstance) -> set[str]:
    s = subject_of(mem)
    return {e.lower() for e in (mem.entities or []) if e.lower() != s}


def _ts(mem: MemoryInstance) -> int:
    return (mem.event_ts_ms if mem.event_ts_ms is not None
            else mem.created_ts_ms or 0)


def _slot(mem: MemoryInstance) -> str | None:
    """属性槽（治理 v2 _slot_key 同源）：属性实体 entities[1]，否则 topic(≠主语)。"""
    s = subject_of(mem)
    if len(mem.entities or []) >= 2:
        return mem.entities[1].lower()
    if mem.topic and (s is None or mem.topic.lower() != s):
        return mem.topic.lower()
    return None


def _conflict(a: MemoryInstance, b: MemoryInstance) -> bool:
    """高置信冲突（v2）：共享非主体实体 / 同属性槽 / 共享属性相关词，三者任一。"""
    if bool(_attr(a) & _attr(b)):
        return True
    ka, kb = _slot(a), _slot(b)
    if ka and ka == kb:
        return True
    return bool(_terms(a) & _terms(b))


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
