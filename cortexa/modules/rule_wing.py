"""规则识别分流（业务文档 8.3 规则专属 Wing / ADR-0003 规则引擎优先）。

W2 管线第 1 步：为每条消息判定 MemoryType 与知识拓扑定位（wing/room）。
- 规则引擎优先：零 token、毫秒级、可解释（写入 token 预算红线的守护者）
- LLM 兜底：规则层落到默认 FACT 且 settings.rule_llm_fallback 开启时，
  经 LLMClient（固定 gpt-4o-mini，官方硬规则）做一次分类；不可用即降级

误报/漏报取向：RULE 偏宽（维度 F 是七维最弱项，宁多收进永不衰减 Wing）；
其余类型偏保守（漏检落 FACT 无衰减风险）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..llm import LLMClient
from ..models import MemoryType


@dataclass(frozen=True)
class Classification:
    """单条消息的识别结论。source 标注判定来源（rule/llm/default）保证可解释。"""

    mem_type: MemoryType
    confidence: float
    source: str  # "rule" | "llm" | "default"
    room: str


_ROOMS: dict[MemoryType, str] = {
    MemoryType.RULE: "constraints",
    MemoryType.PREFERENCE: "personal",
    MemoryType.EVENT: "events",
    MemoryType.PROCEDURE: "howto",
    MemoryType.CODE_DEBUG: "code",
    MemoryType.CODE_DEV: "code",
    MemoryType.FACT: "general",
}

# 每类一组正则，顺序即优先级（先专后泛）。中文词直接子串匹配（\b 对中文无效）。
_RULE_STRONG = re.compile(
    r"(\bmust(?! have)\b|\bmustn'?t\b|\bnever\b|\bforbidden\b|\bprohibited\b"
    r"|\bnot allowed\b|\bdo not\b|\bdon'?t\b|\bcannot\b|\bcan'?t(?! be)\b"
    r"|\bshall not\b|\brequired\b|\bhas to\b|\bhave to\b"
    r"|必须|禁止|不要|不能|不得|务必|切记|严禁|永远不要|永远不|一律|规则是)",
    re.IGNORECASE,
)
_RULE_DIRECTIVE = re.compile(r"(?:^|[.!?;]\s+)always\s+\w+", re.IGNORECASE)
_RULE_NOUN = re.compile(
    r"(\bpolicy\b|\brules?\b|\bguideline\b|\bconvention\b|协议|规定|约定|规范)",
    re.IGNORECASE,
)

_PREFERENCE = re.compile(
    r"(\bprefers?\b|\bpreference\b|\bfavorite\b|\bfavourite\b|\bhates?\b|\benjoys?\b"
    r"|\ballergic\b|\bdislikes?\b|\bcan'?t stand\b"
    r"|\b(?:i|we)\s+(?:really\s+|usually\s+)?(?:like|love)\b"
    r"|喜欢|讨厌|偏好|最爱|最喜欢|不喜欢|过敏|钟爱)",
    re.IGNORECASE,
)
_PROCEDURE = re.compile(
    r"(\bhow to\b|\bstep[- ]?by[- ]?step\b|\bstep \d\b|\btutorial\b|\binstructions?\b"
    r"|步骤|第一步|教程|指南|怎么(?:才能|做))",
    re.IGNORECASE,
)
_EVENT = re.compile(
    r"(\bmoved to\b|\bmoved\b|\badopted\b|\bborn in\b|\bgraduated\b|\bgot married\b"
    r"|\bgot engaged\b|\battended\b|\btraveled\b|\bvisited\b|\bwent to\b"
    r"|\bstarted (?:working|studying)\b|\bquit\b|\bretired\b|\bpassed away\b"
    r"|\bbought\b|\bsold\b|\bbroke up\b"
    r"|搬到|毕业于|结婚|出生|辞职|退休|去世|买了|卖(?:了|掉)|分手)",
    re.IGNORECASE,
)
_CODE_DEBUG = re.compile(
    r"(\berrors?\b|\bexception\b|\btraceback\b|\bstack trace\b|\bcrash(?:es|ed)?\b"
    r"|\bbugs?\b|\bfails?\b|\bfailed\b|\bfailure\b|\bnot working\b|\bbroken\b"
    r"|报错|异常|崩溃|堆栈|抛出错误)",
    re.IGNORECASE,
)
_CODE_DEV = re.compile(
    r"(\bcommits?\b|\bcommit(?:ted)?\b|\bpush(?:ed)?\b|\bmerge(?:d)?\b|\bbranch(?:es)?\b"
    r"|\brebase\b|\bpull request\b|\bdeploy(?:s|ed|ment)?\b|\brelease(?:d)?\b"
    r"|\bgit checkout\b"
    r"|提交代码|部署|合并到|分支|发布版本)",
    re.IGNORECASE,
)


def classify(content: str) -> Classification:
    """规则引擎分类（零 LLM）。优先级：RULE > CODE_DEBUG > CODE_DEV
    > PREFERENCE > PROCEDURE > EVENT > FACT（默认兜底）。"""
    text = content or ""
    if _RULE_STRONG.search(text) or _RULE_DIRECTIVE.search(text) or _RULE_NOUN.search(text):
        return Classification(MemoryType.RULE, 0.85, "rule", _ROOMS[MemoryType.RULE])
    if _CODE_DEBUG.search(text):
        return Classification(MemoryType.CODE_DEBUG, 0.85, "rule", _ROOMS[MemoryType.CODE_DEBUG])
    if _CODE_DEV.search(text):
        return Classification(MemoryType.CODE_DEV, 0.8, "rule", _ROOMS[MemoryType.CODE_DEV])
    if _PREFERENCE.search(text):
        return Classification(MemoryType.PREFERENCE, 0.8, "rule", _ROOMS[MemoryType.PREFERENCE])
    if _PROCEDURE.search(text):
        return Classification(MemoryType.PROCEDURE, 0.75, "rule", _ROOMS[MemoryType.PROCEDURE])
    if _EVENT.search(text):
        return Classification(MemoryType.EVENT, 0.8, "rule", _ROOMS[MemoryType.EVENT])
    return Classification(MemoryType.FACT, 0.5, "default", _ROOMS[MemoryType.FACT])


_LLM_SYSTEM = (
    "You classify a chat memory into exactly one type: "
    "RULE, PREFERENCE, EVENT, PROCEDURE, CODE_DEBUG, CODE_DEV, FACT. "
    "RULE = constraints/instructions the user must follow. "
    "Reply with the type name only."
)


def classify_message(
    content: str, llm: LLMClient | None, allow_llm: bool = True
) -> Classification:
    """管线入口：规则引擎优先，默认 FACT 时按需走 LLM 兜底（不可用即降级）。"""
    base = classify(content)
    # LLM 兜底可行性由档案驱动：推理模型（deepseek/kimi）reasoning 慢，
    # 无法纳入 Add 红线 -> can_sync_classify()=False 时纯规则（RULE 偏宽设计）
    llm_ok = (
        allow_llm
        and llm is not None
        and llm.available()
        and llm.can_sync_classify()
    )
    if base.source == "rule" or not llm_ok:
        return base
    # Add 红线内兜底：紧超时 + 档案抬升 max_tokens（推理余量）
    reply = llm.chat(
        _LLM_SYSTEM, content, max_tokens=8, use_classify_budget=True
    )
    if reply:
        token = reply.strip().strip(".").upper()
        try:
            mem_type = MemoryType(token)
        except ValueError:
            return base
        return Classification(mem_type, 0.75, "llm", _ROOMS[mem_type])
    return base


def route_wing(cls: Classification) -> str:
    """知识拓扑 Wing 路由：RULE 独立成 Wing（永不衰减区），其余入 general。"""
    return "rules" if cls.mem_type == MemoryType.RULE else "general"
