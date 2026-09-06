"""T1.1 J1：确定性查询规划（CHM P1 的确定性移植，2026-09-06）。

把单条查询解拆为 core_terms/entities/temporal_cues/evidence_needs 字段。
字段消费方：entities→entity 特征通道(search 层)；temporal_cues→时间过滤(F2 层，
待接入)；core_terms 本身即词法通道输入（不重复消费）；evidence_needs 为
CHM 结构化规划的占位（确定性暂给空，语义版需 LLM/后续）。零 LLM、零新增 Search 调用。
"""

from __future__ import annotations

import re

from .encoding import extract_entities

_STOP = frozenset({
    "the", "what", "did", "does", "is", "are", "my", "i", "how", "many",
    "when", "where", "which", "a", "an", "of", "to", "in", "on", "for", "and",
    "do", "was", "were", "have", "has", "had", "it", "its", "that", "this",
    "than", "then", "me", "we", "our", "you", "your", "he", "she", "they",
    "them", "their", "am", "be", "been", "not", "no", "now", "currently", "so",
    "with", "at", "by", "about", "since", "ago", "after", "before", "during",
    "going", "want", "would", "could", "should", "can", "will", "need", "please",
    "let", "know", "tell", "say", "said", "mentioned", "remember", "again",
})

_TEMP_PATTERNS = [
    r"\b(19|20)\d{2}\b",
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}",
    r"\b(?:yesterday|last week|last month|last year|前天|昨天|上周|上个月|去年)\b",
    r"\b\d+\s+(?:day|days|week|weeks|month|months|year|years|天|周|个月|年)\s+ago\b",
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b",
]
_TEMP_RE = re.compile("|".join("(?:" + p + ")" for p in _TEMP_PATTERNS), re.IGNORECASE)


def parse_query_plan(query: str) -> dict:
    """确定性查询规划。返回 dict：core_terms/entities/temporal_cues/evidence_needs。"""
    text = query or ""
    low = text.lower()
    core_terms = [
        t for t in re.findall(r"[a-z0-9']+", low)
        if t not in _STOP and len(t) > 2
    ]
    entities = extract_entities(text)
    cues: list[str] = []
    for m in _TEMP_RE.finditer(text):
        cue = m.group(0)
        if cue not in cues:
            cues.append(cue)
    return {
        "intent": "recall",
        "core_terms": core_terms[:12],
        "entities": entities[:8],
        "temporal_cues": cues[:6],
        "evidence_needs": [],
    }