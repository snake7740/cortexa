"""同会话邻接扩展（2026-10-29，AML 榜前三共性证据链）。

来源与证据（本榜榜首开源方案的公开机制说明，另见 docs/METHOD_CARD.md §7）：
- InvMem（开源文本榜第 1，45.06）：融合前 20 种子各补同会话存储序前后各 1 条，
  去重后不足 K 再按原融合序补足（MEMORY_RESULT_WINDOW=1）；
- ActiveMemoryIndex（第 3，44.84）：同机制离线实测 .6333 → .6802（+4.7 分，
  n=1540，配对 +141/−67，p<0.0001，时序类 +5.3），且邻居占用同一批 top_k 名额、
  不扩大条数——在「top_k 只限条数」的契约下是固定预算内换一种花法；
- FlowGrid（第 8）滑动窗口视图、aml-memory-mvp（第 10）adjacent-turn expansion
  同向——本榜相邻上下文恢复是前排共性而非孤立技巧。

解决的问题：检索器命中「是的，我下周就会过去」时，「去哪里」在上一话轮；
命中结论时依赖的条件在紧邻片段。单条消息常不自足，阅读模型无法还原从未
发给它的东西（AMI 原文）。

不变量（与契约/纪律对齐）：
- 只从 ACTIVE、同 scope、同 session 的时间线取邻居（active_timeline），零跨用户
  泄漏；邻居是真实原始记忆条目，非生成内容（红线：Search 只返回证据）。
- 种子相对次序与列表其余部分的原融合序不变；邻居紧跟种子、按时间序放置
  （前邻 → 种子 → 后邻），服务时序类提升。
- 返回列表长度恒 ≤ top_k（邻居占同批名额；候选不足时与旧行为一致填满不满）。
- 纯读：不写任何状态（与参赛档 Search 纯函数口径一致）。
"""

from __future__ import annotations

from ..models import MemoryInstance
from ..storage import MemoryStore


def apply_adjacency(
    scored: list[tuple[MemoryInstance, float]],
    store: MemoryStore,
    scope: str,
    *,
    k: int,
    seeds: int = 20,
    radius: int = 1,
) -> list[tuple[MemoryInstance, float]]:
    """对已排序候选做同会话 ±radius 邻接扩展，返回长度 ≤ k 的新序列表。

    scored: 融合排序后的 (mem, score) 降序列表；
    种子取前 min(seeds, k) 个返回位次（InvMem 取融合前 20）；每个种子的邻居
    按时间序插到种子自身之前/之后，已被占用（种子或先前邻居）的不重复取；
    溢出部分按原序丢弃以守住 k（AMI：邻居与尾部低分候选争同一批名额）。
    """
    if not scored or radius <= 0 or k <= 0:
        return scored

    timeline = store.active_timeline(scope)
    if not timeline:
        return scored
    pos = {mid: i for i, (_sid, mid) in enumerate(timeline)}

    # 按会话分组的时间线索引（邻居必须同 session；None 会话视作同一分组，与
    # ReFind 会话级聚合口径一致）
    session_of = [sid for sid, _mid in timeline]

    seed_n = min(seeds, k, len(scored))
    # 先定种子集合（决定谁有邻居资格），按原序取前 seed_n
    seed_ids = {scored[i][0].id for i in range(seed_n)}
    seed_scores = {scored[i][0].id: scored[i][1] for i in range(seed_n)}

    consumed = set(seed_ids)
    out: list[tuple[MemoryInstance, float]] = []
    for idx, (mem, score) in enumerate(scored):
        if idx < seed_n:
            i = pos.get(mem.id)
            if i is not None:
                sid = session_of[i]
                # 前邻（时间序在前），按距离从近到远；取最先未被占用的 radius 个
                for dist in range(1, radius + 1):
                    j = i - dist
                    if j < 0 or session_of[j] != sid:
                        break
                    nid = timeline[j][1]
                    if nid in consumed:
                        continue
                    consumed.add(nid)
                    nmem = store.get(nid, scope)
                    if nmem is not None:
                        out.append((nmem, score))
                # 后邻放在种子之后收集（先记录，种子 emit 后插入）
            out.append((mem, score))
            if i is not None:
                sid = session_of[i]
                for dist in range(1, radius + 1):
                    j = i + dist
                    if j >= len(timeline) or session_of[j] != sid:
                        break
                    nid = timeline[j][1]
                    if nid in consumed:
                        continue
                    consumed.add(nid)
                    nmem = store.get(nid, scope)
                    if nmem is not None:
                        out.append((nmem, seed_scores.get(mem.id, score)))
        else:
            if mem.id in consumed:
                continue
            out.append((mem, score))
    return out[:k]
