"""L2 领域模型层：消费 L3 schema 的通用抽取/状态机算法（场景无关）。"""

from .cast import CastRegistry
from .facts import NARRATION, QUOTE, RECALL, REPORT, FactExtractor
from .governance import GovernanceEngine
from .lifecycle import LifecycleEngine

__all__ = [
    "NARRATION",
    "QUOTE",
    "RECALL",
    "REPORT",
    "CastRegistry",
    "FactExtractor",
    "GovernanceEngine",
    "LifecycleEngine",
]
