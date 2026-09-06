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


# 属性状态动词/短语（v3，2026-09-06）：证明两句谈同一属性的更新——主语本人
# 属性的『值』随版本变化（住所/雇主/颜色/供应商）。与 _GENERIC 的差别：v3 不再
# 把这类动词一律当噪声，而是『短语命中 + 值实体不同 + 非属格转移』三者同判，
# 既保住 Seattle/Boston 类真冲突，又挡下 "Alice works at Google" vs
# "Alice's brother works at Microsoft"（brother 句含属格转移，subject 实为兄弟）。
_ATTR_PHRASES = (
    "moved to", "living in", "lives in", "works at", "working at", "work at",
    "joined", "selected", "favorite", "favourite", "prefers", "preferred",
    "currently", "likes", "liked", "started at", "bought", "adopted",
    "changed", "switched to", "is now", "has been", "upgraded", "renamed",
)


def _attr_statement(mem: MemoryInstance) -> bool:
    """主语本人属性的状态陈述：无『subject's X』属格转移且命中属性短语。"""
    low = (mem.content or "").lower()
    s = subject_of(mem)
    if s and s.split()[0] + "'s" in low:
        return False  # "alice's brother/mail/office..." 属性属于他人
    return any(p in low for p in _ATTR_PHRASES)


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
    """高置信冲突（v3，temporal_conflict_eval_v1）：四信号任一。

    1) 共享非主体实体（同值实体——同属性同值非冲突但同槽高频）；
    2) 同属性槽（entities[1]/topic）；
    3) 双方都是『主语本人属性状态陈述』且值实体不同（住所/雇主版本史）；
    4) 共享属性相关词（兜底，如 favorite/color）。
    属格转移（alice's brother works at microsoft）挡在 _attr_statement 层。
    """
    if bool(_attr(a) & _attr(b)):
        return True
    ka, kb = _slot(a), _slot(b)
    if ka and ka == kb:
        return True
    va, vb = _attr(a), _attr(b)
    if (va and vb and va != vb and _attr_statement(a) and _attr_statement(b)):
        return True
    return bool(_terms(a) & _terms(b))


def _conflicted_clusters(scored: list, top_n: int) -> list[tuple[int, ...]]:
    """返回需要仲裁的冲突簇（位置元组，含 ≥2 成员、存在冲突对、时间可分）。"""
    clusters: dict[str, list[int]] = {}
    for i, (mem, _s) in enumerate(scored[:top_n]):
        s = subject_of(mem)
        if s:
            clusters.setdefault(s, []).append(i)
    conflicted: list[tuple[int, ...]] = []
    for idxs in clusters.values():
        if len(idxs) < 2:
            continue
        mems = [scored[i][0] for i in idxs]
        if not any(_conflict(mems[a], mems[b])
                   for a in range(len(mems)) for b in range(a + 1, len(mems))):
            continue
        if len({_ts(m) for m in mems}) < 2:
            continue
        conflicted.append(tuple(sorted(idxs)))
    return conflicted


def arbitrate(scored: list, top_n: int = 50, keep_prev: int = 0) -> list:
    """冲突簇内按事件时间重排(最新在前)；无冲突簇不动。

    keep_prev=0：reorder only（所有簇成员保留，仅调序）——C3 档。
    keep_prev>0：C4 档——簇内只保留最新 + 至多 keep_prev 个最近的 predecessor，
    更旧版本从返回中剔除（版本史压缩，防列表题 no-extras 污染）。返回新列表。
    """
    out = list(scored)
    for idxs in _conflicted_clusters(out, top_n):
        if len(idxs) < 2:
            continue
        ordered = sorted(
            [(out[i], i) for i in idxs],
            key=lambda pair: (_ts(pair[0][0]), pair[1]),
            reverse=True,
        )
        for pos, (pair, _orig_i) in zip(sorted(idxs), ordered):
            out[pos] = pair
        if keep_prev > 0:
            # 剔除按 id 而非位置（重排后下标指向的内容已变化——2026-09-06 实测踩坑；
            # 注意 ordered 元素=(mem,score,orig_i) 解包后 pair=(mem,score)，mem=pair[0]）
            keep_ids = {pair[0].id for pair, _i in ordered[:1 + keep_prev]}
            drop_ids = {pair[0].id for pair, _i in ordered} - keep_ids
            if drop_ids:
                out = [x for x in out if x[0].id not in drop_ids]
                # 列表缩短：后续簇用新列表重扫（递归，drop 为空即终止）
                return arbitrate(out, top_n=top_n, keep_prev=keep_prev)
    return out


def cluster_report(scored: list, top_n: int = 50) -> list[dict]:
    """簇观测（temporal_conflict_eval_v1 仪器化）：返回冲突簇成员 id 清单。

    供 false_conflict_rate 统计——truth 表交叉判定哪些簇是真属性冲突、哪些是
    误聚（非冲突事实进了冲突簇）。
    """
    out = []
    for idxs in _conflicted_clusters(scored, top_n):
        ordered = sorted(
            (scored[i][0] for i in idxs),
            key=lambda m: _ts(m),
            reverse=True,
        )
        out.append({
            "subject": subject_of(ordered[0]),
            "members": [m.id for m in ordered],
        })
    return out
