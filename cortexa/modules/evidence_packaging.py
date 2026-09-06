"""Q8 Evidence Packaging（2026-09-06，外部复核 v3 §12 立项）。

问题：top_k=100 之下，每个 evidence 槽里"放什么"本身就是优化变量——Answer 模型
看到的是证据文本而非内部分数。本模块把包装做成纯函数：给定返回证据（按返回序）与
会话时间线，把每个槽的内容从"单条原文"变换为带邻接支撑的文本段。

档位（对照 v3 实验矩阵在 Cortexa 单 RAW 表上的映射）：
    raw       原文不变（membership/order/token 全零变化——默认）
    pm1       RAW + 前 1 条邻接（上一轮上下文）
    pm2       RAW + 前 2 条邻接
    raw-atomic RAW-first + support 最小形态：RAW + 前 1 条邻接。Cortexa 无原子
              fact 表（raw-first 设计，AMI 双写 raw+fact 的本地等价口径），
              "原子支持"用相邻轮内容近似并在此文档标注，不做虚假的 atomic 声明。

不变量：membership（返回哪些证据）与 order（什么顺序）完全不变——本模块只改变
槽内文本，供回放器统计送答模型的 token/覆盖代理，判答层接入后裁决是否进默认。
确定性纯函数；examples/aml_replay.py --packaging 消费。
"""

from __future__ import annotations

MODES = ("raw", "pm1", "pm2", "raw-atomic")


def pack_question(evidences: list, order_ids: list[str],
                  content_of, mode: str = "raw") -> tuple[list[str], int]:
    """包装一个问题的最终证据列表。

    evidences: 检索返回的证据（按返回序，带 .id/.content）。
    order_ids: 该 scope 会话内全部记忆 id（rowid 保序，时间线）。
    content_of: id -> content | None（邻接内容解析器）。
    mode: raw/pm1/pm2/raw-atomic。
    返回 (包装后文本列表, 附加字符总数)。包装文本仅供统计，不改召回/排序。
    """
    if mode not in MODES:
        raise ValueError(f"packaging mode 未知: {mode}")
    if mode == "raw":
        return [e.content for e in evidences], 0
    win = 2 if mode == "pm2" else 1
    pos = {mid: i for i, mid in enumerate(order_ids)}
    out: list[str] = []
    added = 0
    for e in evidences:
        parts = [e.content]
        i = pos.get(e.id)
        if i is not None:
            # 仅前文邻接、就近在前（紧邻轮上下文优先于更早轮）
            for j in range(i - 1, max(-1, i - 1 - win), -1):
                c = content_of(order_ids[j])
                if c:
                    parts.append(c)
                    added += len(c)
        out.append("\n".join(parts))
    return out, added
