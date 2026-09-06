"""FactExtractor（L2 领域模型）：结构化事实 + 叙述语境分层。

设计文档：docs/design/scene-aware-architecture.md（v2.1 §5 语境分层）

解决的问题（图分析 + 生命周期结算实证暴露）：
- "赵铁柱死了"（作者叙述 NARRATION）= 高置信事实，可驱动 EXITED
- "叶长生说赵铁柱死了" / "'赵铁柱死了。'周元白说"（QUOTE/转述）= 不是事实，不驱动状态
- "他想到赵铁柱" / "等赵铁柱的死因"（RECALL/回忆）= 引用，不表示活体在场

语境分类（对每个实体的每次出现判定）：
  NARRATION  作者叙述（该实体本体行为/被描述）→ 活体在场
  QUOTE      引号内（他人在说话中提到）→ 非该实体活体动作
  RECALL     心理/回忆（想/记得/听说 等引导）→ 非活体在场
  REPORT     转述（X说Y...）→ 视 Y 是否实指而定（保守不算在场）

纯确定性实现（零 LLM，预算内可用）。场景词表由 L3 schema 提供。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schema import SceneSchema, get_schema

# 语境分类
NARRATION = "NARRATION"
QUOTE = "QUOTE"
RECALL = "RECALL"
REPORT = "REPORT"

# 引号字符对（中文弯引号 + ASCII + 直角引号）
_QUOTE_PAIRS = [
    ("\u201c", "\u201d"),  # "" 中文弯
    ('"', '"'),  # ASCII 直引号
    ("\u300c", "\u300d"),  # 「」
]

# 回忆/心理引导词（前 12 字内出现 → RECALL）
# 覆盖图分析实证案例："等王宣出手"(ch51 叶长生回忆当年) / "想象一个人在黑暗里等" / "感觉"
_RECALL_TRIGGERS = re.compile(
    r"(?:想|想起|想到|记得|回忆|回想|怀念|以为|觉得|认为|听说|听闻|"
    r"提到|提及|谈起|念叨|惦记|梦到|梦见|料想|猜想|推测|"
    r"等|等过|想象|感觉|那年|当年|那时|曾经|从前|过去)"
)
# 转述引导词（X 说/道/问...Y）
_REPORT_TRIGGERS = re.compile(r"(?:说|说道|道|问|答|告诉|称|宣称|喊道|低声道|沉声道)")
# 名词性死亡引用（X的死/尸体/遗物…）
_NOUN_DEATH = re.compile(r"(?:的)?(?:死|尸首|尸体|坟墓|墓碑|遗物|遗言|遗骸|死讯|丧命|当年|生前|之死)")

# 中文引号判定：ASCII " 或中文弯/直角
_CHINESE_QUOTE_OPEN = frozenset(('"', "\u201c", "\u300c", "「"))
_CHINESE_QUOTE_CLOSE = frozenset(('"', "\u201d", "\u300d", "」"))


@dataclass(frozen=True)
class EntityOccurrence:
    """一个实体在文本中的一次出现（带语境归属）。"""

    entity: str
    context: str  # NARRATION/QUOTE/RECALL/REPORT
    position: int
    is_presence: bool  # 是否活体在场（NARRATION 且非名词引用）


def _in_quotes(text: str, pos: int) -> bool:
    """判断 pos 是否在引号对内（任一种引号）。"""
    # 简化：统计 pos 前未闭合的引号
    depth = 0
    i = 0
    while i < pos and i < len(text):
        ch = text[i]
        if ch in _CHINESE_QUOTE_OPEN:
            depth += 1
        elif ch in _CHINESE_QUOTE_CLOSE:
            depth = max(0, depth - 1)
        i += 1
    return depth > 0


class FactExtractor:
    """按语境分层抽取实体出现的事实归属（零 LLM）。"""

    def __init__(self, schema: SceneSchema | None = None) -> None:
        self._schema = schema or get_schema("novel")

    @property
    def schema(self) -> SceneSchema:
        return self._schema

    def occurrences(self, text: str, entities: list[str]) -> list[EntityOccurrence]:
        """扫描文本，返回所有实体的出现（带语境）。

        entities: 已知实体名单（Cast/已知角色）。只处理名单内，不发明新实体。
        """
        out: list[EntityOccurrence] = []
        if not entities:
            return out
        # 只取最长名匹配（防"叶长生"与"叶长生把"重叠——用正向后行断言避免嵌套）
        for ent in sorted(set(entities), key=len, reverse=True):
            for m in re.finditer(re.escape(ent), text):
                pos = m.start()
                context = self._classify_context(text, pos, ent)
                presence = self._is_presence(text, pos, ent, context)
                out.append(EntityOccurrence(ent, context, pos, presence))
        return sorted(out, key=lambda o: o.position)

    def _classify_context(self, text: str, pos: int, ent: str) -> str:
        """判定实体出现在 pos 的语境。"""
        if _in_quotes(text, pos):
            return QUOTE
        # 前 20 字找回忆/心理引导
        head = text[max(0, pos - 20):pos]
        if _RECALL_TRIGGERS.search(head):
            return RECALL
        # 前 4 字找转述引导（X说...，实体作为内容被说出）
        head4 = text[max(0, pos - 4):pos]
        if _REPORT_TRIGGERS.search(head4):
            return REPORT
        return NARRATION

    def _is_presence(self, text: str, pos: int, ent: str, context: str) -> bool:
        """该出现是否表示实体活体在场。

        NARRATION 语境下仍排除名词性引用（"赵铁柱的死"）与"X的N"所有格。
        """
        if context != NARRATION:
            return False
        # 名词性引用：X的死/尸体/遗物（实体后紧跟名词死亡词）
        after = text[pos + len(ent):pos + len(ent) + 8]
        if _NOUN_DEATH.search(after):
            return False
        # 实体的"被提及"标记：X的名字/关于X/提到X 等
        before = text[max(0, pos - 6):pos]
        if re.search(r"(?:的名字|关于|提及|关于$)", before):
            return False
        return True

    def is_present(self, text: str, ent: str, entities: list[str]) -> bool:
        """实体是否在文本中活体在场（任一 NARRATION 出现）。"""
        return any(
            o.entity == ent and o.is_presence
            for o in self.occurrences(text, entities)
        )
