"""T1.3 H1：会话级 RRF 第二项（ReFind cluster boost 移植，2026-09-06）。

RRF 经典项 r1 = 1/(k+rank_turn)。H1 增加 r2：把候选按 session 聚合并按
成员融合分总和排序，给会话赋名次后每成员加 1/(k+rank_session)（权重可调）。
默认关（settings.session_rrf_enabled=False），开启后仍只影响排序不改召回集。
"""

from __future__ import annotations

from cortexa.models import MemoryInstance


def session_rrf_bonus(
    fused_hits: dict[str, tuple[MemoryInstance, float]],
    *,
    k: int = 60,
    weight: float = 1.0,
) -> dict[str, float]:
    """按候选的 session 聚合名次计算每 id 的 r2 贡献（纯函数，可单测）。

    session 键取 mem.session_id，缺省回退 scope；同 id 只计一次。
    会话总分=成员融合分之和；按总分降序编号（从 1），成员加 weight/(k+rank)。
    """
    sess_sum: dict[str, float] = {}
    sess_mems: dict[str, list[str]] = {}
    for mid, (mem, score) in fused_hits.items():
        key = (mem.session_id or mem.scope or "x")
        sess_sum[key] = sess_sum.get(key, 0.0) + score
        sess_mems.setdefault(key, []).append(mid)
    order = [s for s, _ in sorted(sess_sum.items(), key=lambda kv: -kv[1])]
    rank_of = {s: i + 1 for i, s in enumerate(order)}
    bonus: dict[str, float] = {}
    for s, mids in sess_mems.items():
        add = weight / (k + rank_of[s])
        for mid in mids:
            bonus[mid] = add
    return bonus


def apply_session_bonus(
    scored: list[tuple[MemoryInstance, float]],
    bonus: dict[str, float],
) -> list[tuple[MemoryInstance, float]]:
    """把 r2 bonus 加到最终分（返回新列表，不就地改）。"""
    return [(mem, score + bonus.get(mem.id, 0.0)) for mem, score in scored]