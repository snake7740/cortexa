"""LifecycleEngine（L2 领域模型）：schema.transitions 驱动的通用生命周期状态机。

设计文档：docs/design/scene-aware-architecture.md（v2.1 §4 transitions）

与 novel_adapter._is_valid_transition 的关系：后者是小说硬编码的状态转移表；
本引擎把"事件→状态迁移"完全交给 schema 声明（L3 数据），引擎本身场景无关——
novel/aml/code 三场景共享同一状态机逻辑，差异只在各自 schema。

防幻觉约束（Review 修正 3 的落地）：
- context_required 事件（如 death）仅 NARRATION 语境可驱动迁移；
- 迁移非法时返回 rejected（不猜测、不静默改状态）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..schema import SceneSchema, get_schema


@dataclass(frozen=True)
class TransitionResult:
    """一次事件应用的结果。"""

    allowed: bool
    from_state: str | None
    to_state: str | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.allowed


class LifecycleEngine:
    """schema.transitions 驱动的事件→状态机（纯逻辑，不碰存储）。"""

    def __init__(self, schema: SceneSchema | None = None) -> None:
        self._schema = schema or get_schema("novel")

    @property
    def schema(self) -> SceneSchema:
        return self._schema

    def validate_transition(
        self, event: str, from_state: str | None, *, context: str = "NARRATION"
    ) -> TransitionResult:
        """事件能否把实体从 from_state 迁出。

        from_state=None 表示新实体（注册），不校验迁移——由调用方按 schema 初始状态处理。
        context: NARRATION | QUOTE | RECALL | REPORT。
        """
        if from_state is None:
            return TransitionResult(allowed=True, from_state=None, reason="new entity")

        trans = self._schema.transitions.get(event)
        if trans is None:
            return TransitionResult(
                allowed=False, from_state=from_state,
                reason=f"事件 {event} 未在 schema.transitions 声明",
            )
        # 语境约束：context_required 事件（如 death）仅 NARRATION 可驱动
        if event in self._schema.context_required and context != "NARRATION":
            return TransitionResult(
                allowed=False, from_state=from_state,
                reason=f"{event} 要求 NARRATION 语境，实际 {context}",
            )
        allowed_from = trans.get("from", ())
        if from_state not in allowed_from:
            return TransitionResult(
                allowed=False, from_state=from_state,
                reason=f"{event}: 不允许从 {from_state} 迁移（允许: {allowed_from}）",
            )
        return TransitionResult(
            allowed=True, from_state=from_state, to_state=trans["to"]
        )

    def apply_event(
        self,
        event: str,
        from_state: str | None,
        *,
        context: str = "NARRATION",
    ) -> TransitionResult:
        """尝试应用事件。非法迁移返回 rejected（调用方不得静默改状态）。"""
        return self.validate_transition(event, from_state, context=context)

    def initial_state(self, entity_type: str) -> str | None:
        """新实体的初始状态（取该类型生命周期第一个状态）。"""
        states = self._schema.lifecycle_states.get(entity_type)
        return states[0] if states else None
