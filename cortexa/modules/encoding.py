"""L4 编码层（业务文档 7.1 管线）：识别结论 -> MemoryInstance 组装。

向量化不在本模块（由 LLMClient.embed 批量完成，调用方 server 负责批量），
本模块只做结构化标注：实体/主题/意图/置信度/拓扑定位/去重指纹。
LLM 精细标注（RARE 重编码）属 P4，W2 全部启发式规则。
"""

from __future__ import annotations

import re

from ..models import AmlMessage, MemoryInstance, MemoryType, content_hash
from .rule_wing import Classification, route_wing

_ENTITY_PHRASE = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)*)\b")
_ENTITY_QUOTED = re.compile(r"[\"'“「]([^\"'”」]{1,64})[\"'”」]")
_ENTITY_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# 句首冠词/代词/助词不算实体（短语首词命中即丢弃整个短语，MVP 取舍）
_ENTITY_STOP = {
    "the", "this", "that", "there", "then", "they", "these", "those",
    "when", "what", "where", "how", "why", "who", "if", "it", "its",
    "we", "you", "your", "my", "our", "she", "he", "his", "her",
    "in", "on", "at", "to", "from", "with", "and", "but", "for", "not",
    "a", "an", "as", "by", "or", "so", "do", "does", "did", "is", "are",
    "was", "were", "be", "been", "am", "can", "will", "would", "should",
    "could", "may", "might", "must", "have", "has", "had", "get", "got",
    "never", "always", "step", "first", "next", "last", "user", "assistant",
}

INTENT_BY_TYPE: dict[MemoryType, str] = {
    MemoryType.RULE: "constraint",
    MemoryType.PREFERENCE: "preference_statement",
    MemoryType.EVENT: "life_event",
    MemoryType.PROCEDURE: "howto",
    MemoryType.CODE_DEBUG: "debug_note",
    MemoryType.CODE_DEV: "dev_workflow",
    MemoryType.FACT: "fact_statement",
}


def extract_entities(text: str) -> list[str]:
    """启发式实体抽取：邮箱 > 引号内容 > 大写短语。保序去重，上限 8 个。

    中文专名识别不在 W2 范围（LLM 标注属 P4 精炼器）。
    """
    found: list[str] = []
    for m in _ENTITY_EMAIL.finditer(text):
        found.append(m.group(0))
    for m in _ENTITY_QUOTED.finditer(text):
        phrase = m.group(1).strip()
        if phrase:
            found.append(phrase)
    for m in _ENTITY_PHRASE.finditer(text):
        phrase = m.group(1).strip()
        if phrase.split()[0].lower() in _ENTITY_STOP:
            continue
        found.append(phrase)
    deduped: list[str] = []
    for e in found:
        if e and e not in deduped:
            deduped.append(e)
    return deduped[:8]


def encode_message(
    msg: AmlMessage,
    *,
    scope: str,
    session_id: str | None,
    request_id: str,
    cls: Classification,
) -> MemoryInstance:
    """单条消息 -> MemoryInstance（拓扑定位/标注/去重指纹/FSRS 初值）。"""
    entities = extract_entities(msg.content)
    return MemoryInstance(
        scope=scope,
        session_id=session_id,
        role=msg.role,
        content=msg.content,
        content_hash=content_hash(scope, msg.content),
        mem_type=cls.mem_type,
        event_ts_ms=msg.timestamp,
        valid_from_ms=msg.timestamp,
        wing=route_wing(cls),
        room=cls.room,
        entities=entities,
        topic=entities[0].lower() if entities else "",
        intent=INTENT_BY_TYPE[cls.mem_type],
        confidence=cls.confidence,
        no_decay=cls.mem_type == MemoryType.RULE,
        source_request_id=request_id,
    )
