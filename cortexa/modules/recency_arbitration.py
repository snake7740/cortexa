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


def _components(scored: list, top_n: int) -> list[dict]:
    """冲突图连通分量（v4，2026-09-06 side-effect 审计修复）。

    原实现按 subject 分组：同 subject 组内只要存在一对真冲突，整组全部按时间重排——
    非冲突成员（属格 decoy/跨属性句/共享词伪边句）被顺带扫动，这正是 T6 在
    false_conflict=0 时仍 0/10 的根因。v4 改为：同 subject 组内以 pairwise _conflict
    建图取连通分量，只有分量成员参与仲裁；无冲突边的成员位置严格不动
    （不变量：non_conflict_candidate_order_changed = 0）。
    """
    from collections import defaultdict
    subjects: dict[str, list[int]] = defaultdict(list)
    for i, (mem, _s) in enumerate(scored[:top_n]):
        s = subject_of(mem)
        if s:
            subjects[s].append(i)
    out: list[dict] = []
    for s, idxs in subjects.items():
        if len(idxs) < 2:
            continue
        parent = list(range(len(idxs)))
        def _find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                if _conflict(scored[idxs[a]][0], scored[idxs[b]][0]):
                    ra_, rb_ = _find(a), _find(b)
                    if ra_ != rb_:
                        parent[rb_] = ra_
        groups: dict[int, list[int]] = defaultdict(list)
        for k in range(len(idxs)):
            groups[_find(k)].append(k)
        for root, ks in groups.items():
            if len(ks) < 2:
                continue  # 单点（无冲突边成员）不仲裁
            comp_idxs = sorted(idxs[k] for k in ks)
            mems = [scored[i][0] for i in comp_idxs]
            if len({_ts(m) for m in mems}) < 2:
                continue  # 同时刻陈述无可分先后
            out.append({"subject": s, "idxs": comp_idxs, "members": [m.id for m in mems]})
    return out


# 过去日期标记（G3 list-suppress 用）：内容含 "in YYYY" 的成员视为已归档的旧列表
# 版本（"worked with vendors A and B in 2024"）；"since YYYY/is working since" 类
# 当前持续态不带 in-年份，不被误杀。
_PAST_YEAR = re.compile(r"\bin\s+(19|20)\d{2}\b")


def arbitrate(scored: list, top_n: int = 50, keep_prev: int = 0,
              list_mode: bool = False) -> list:
    """冲突分量内按事件时间重排(最新在前)；无冲突边成员位置不动（v4）。

    keep_prev=0：reorder only（分量成员保留，仅调序）——C3 档。
    keep_prev>0：C4 档——分量内只保留最新 + 至多 keep_prev 个最近 predecessor，
    更旧版本从返回中剔除（版本史压缩）。剔除按 id（位置会因重排失效——实测踩坑）。
    list_mode=True（RAR-002 G3）：LIST_CURRENT 查询——最新成员恒保留；其余成员
    若带『in YYYY』过去日期标记则剔除（旧列表版本不塞进当前列表），不带标记的
    保留（"since YYYY" 的当前持续态成员仍是当前列表的一部分）。
    """
    out = list(scored)
    for comp in _components(out, top_n):
        idxs = comp["idxs"]
        mems = [out[i][0] for i in idxs]
        ts_sorted = sorted(mems, key=lambda m: (_ts(m), m.id), reverse=True)
        for pos, m in zip(idxs, ts_sorted):
            out[pos] = (m, out[pos][1])
        drop_ids: set[str] = set()
        if list_mode:
            drop_ids = {m.id for m in ts_sorted[1:]
                        if _PAST_YEAR.search(m.content or "")}
        elif keep_prev > 0:
            keep_ids = {m.id for m in ts_sorted[:1 + keep_prev]}
            drop_ids = {m.id for m in ts_sorted} - keep_ids
        if drop_ids:
            out = [x for x in out if x[0].id not in drop_ids]
            return arbitrate(out, top_n=top_n, keep_prev=keep_prev,
                             list_mode=list_mode)
    return out


def cluster_report(scored: list, top_n: int = 50) -> list[dict]:
    """分量观测（v4）：返回将被仲裁的冲突分量成员 id 清单（truth 归因/审计用）。"""
    return [
        {"subject": c["subject"], "members": list(c["members"])}
        for c in _components(scored, top_n)
    ]
