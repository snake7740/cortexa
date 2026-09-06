"""维度地板审计（业务文档 12.x 数学约束的工程落地）。

王虹约束类比：任何写入/压缩/重编码操作不得把结构维度降到地板以下。
- 写入前：enforce() 修补可补维度 + assert_floor() 硬门（W2 接线点 /add）
- 压缩前：P3 Dreaming/精炼器必须先 audit，缺失即拒绝操作

地板定义（计划 W2 契约：RULE wing 必含 RULE/PROCEDURAL/CONFIDENCE）：
- RULE（含 no_decay）：RULE + PROCEDURAL + CONFIDENCE
- 其他记忆：CONTENT（基本健全性）
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import MemoryInstance

RULE_FLOOR = frozenset({"RULE", "PROCEDURAL", "CONFIDENCE"})
GENERIC_FLOOR = frozenset({"CONTENT"})


class DimensionFloorError(ValueError):
    """写入/压缩前维度审计未通过（红线：维度地板 100% 覆盖）。"""


@dataclass(frozen=True)
class FloorReport:
    mem_id: str
    required: frozenset[str]
    missing: frozenset[str]

    @property
    def passed(self) -> bool:
        return not self.missing


def required_dimensions(mem: MemoryInstance) -> frozenset[str]:
    """该实例必须具备的维度集合。no_decay 记忆按 RULE 地板对待（同为红线资产）。"""
    if mem.mem_type.value == "RULE" or mem.no_decay:
        return RULE_FLOOR | GENERIC_FLOOR
    return GENERIC_FLOOR


def audit(mem: MemoryInstance) -> FloorReport:
    """只读审计：返回缺失维度报告（P3 压缩前复用）。"""
    required = required_dimensions(mem)
    missing = required - mem.active_dimensions()
    return FloorReport(mem.id, required, frozenset(missing))


def enforce(mem: MemoryInstance) -> MemoryInstance:
    """写入前修补可补维度（原位语义：调用方传入即持有返回实例）。"""
    if mem.confidence <= 0:
        mem.confidence = 0.5  # CONFIDENCE 维度兜底
    return mem


def assert_floor(mem: MemoryInstance) -> FloorReport:
    """enforce 之后的硬门：不通过即抛 DimensionFloorError（红线，宁拒写不放行）。"""
    report = audit(mem)
    if not report.passed:
        raise DimensionFloorError(
            f"memory {mem.id} missing required dimensions: {sorted(report.missing)}"
        )
    return report
