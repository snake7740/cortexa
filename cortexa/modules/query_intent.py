"""query_intent 确定性分类（RAR-002，2026-09-06）。

零 LLM 词表分诊：CURRENT / LIST_CURRENT(current+enum) / HISTORICAL / SEQUENCE /
ENUM / DEFAULT。原则（v3 外部复核 + RAR 审计共同导出）：
  Temporal intent processing 先于 recency arbitration——
  HISTORICAL -> 年份/相对窗过滤后普通排序（不 RAR）；
  SEQUENCE   -> 关 current-state RAR（顺序问不需要最新在前）；
  CURRENT    -> 可做属性级 recency 仲裁；
  CURRENT+ENUM(LIST_CURRENT) -> 按槽取当前成员，禁止 predecessor 扩张污染列表。
纯函数可单测；词表见外部复核 v3 建议 + 本语料语义约定。
"""

from __future__ import annotations

import re

_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_WORD = re.compile(r"[a-z0-9]+")

# 当前态词（CURRENT）
_CURRENT_EN = frozenset({
    "now", "current", "currently", "latest", "present", "today", "recent", "recently",
})
_CURRENT_CN = ("现在", "当前", "目前", "如今", "最近")
# 历史词（HISTORICAL）——含相对时间/过去式/当年
_HISTORICAL_EN = frozenset({
    "ago", "previously", "used", "formerly", "earlier", "then", "old", "past",
    "before", "since", "last", "previous", "original", "over", "was", "were",
    "had", "did", "finished", "joined", "bought", "moved", "visited", "started",
})
_HISTORICAL_CN = ("曾经", "以前", "当时", "那时候", "原来", "之前", "去年", "上个月", "某年", "某月", "曾")
# 顺序词（SEQUENCE）
_SEQUENCE_EN = frozenset({
    "first", "second", "before", "after", "later", "earliest", "then", "next", "order",
})
_SEQUENCE_CN = ("先", "后", "最先", "然后", "哪个先", "顺序")
# 枚举/列表词（ENUM）
_ENUM_EN = frozenset({
    "which", "all", "list", "every", "each", "who", "what", "names", "name",
})
_ENUM_CN = ("哪些", "所有", "全部", "分别", "都有", "有哪")

# 相对时间片段（F2/R4 已在时间件消费；此处仅用于把『去年/上个月』型问句归 HISTORICAL）
_REL_TAIL = re.compile(r"(last\s+(month|year|week)|ago|上个月|去年|上星期|上周|\d+\s+months?\s+ago)")


def _en_toks(low: str) -> set[str]:
    return set(_WORD.findall(low))


def classify_intent(query: str) -> dict:
    """返回 {kind, flags}；kind ∈ CURRENT/LIST_CURRENT/HISTORICAL/SEQUENCE/ENUM/DEFAULT。"""
    q = query or ""
    low = q.lower()
    toks = _en_toks(low)
    flags: set[str] = set()
    if toks & _CURRENT_EN or any(c in q for c in _CURRENT_CN):
        flags.add("current")
    if _YEAR.search(q) or toks & _HISTORICAL_EN or _REL_TAIL.search(low) \
            or any(c in q for c in _HISTORICAL_CN):
        flags.add("historical")
    if toks & _SEQUENCE_EN or any(c in q for c in _SEQUENCE_CN):
        flags.add("sequence")
    if toks & _ENUM_EN or any(c in q for c in _ENUM_CN):
        flags.add("enum")
    # 优先级：显式年份/相对时间尾（in YYYY / last month / N months ago / 上个月）
    # 强判 HISTORICAL（temporal processing 先行）；否则顺序词优先于泛历史词
    # （"before/after Boston" 是 SEQUENCE 而非 HISTORICAL——两词表共用 before 的歧义）。
    if _YEAR.search(q) or _REL_TAIL.search(low):
        kind = "HISTORICAL"
    elif "sequence" in flags:
        kind = "SEQUENCE"
    elif "historical" in flags:
        kind = "HISTORICAL"
    elif "current" in flags and "enum" in flags:
        kind = "LIST_CURRENT"
    elif "current" in flags:
        kind = "CURRENT"
    elif "enum" in flags:
        kind = "ENUM"
    else:
        kind = "DEFAULT"
    return {"kind": kind, "flags": sorted(flags)}
