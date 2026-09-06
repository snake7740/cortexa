"""R4：时间过滤通道（2026-09-06，纯函数）。
查询带绝对年份线索时，按记忆 event_ts（缺省 created_ts）年份过滤候选，
服务 C 轴"哪一年/何时"类问题。默认关（settings.temporal_filter_enabled）。
"""
from __future__ import annotations

import re
import time

_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def year_cues(text: str) -> list[int]:
    seen: list[int] = []
    for m in _YEAR.finditer(text or ""):
        y = int(m.group(0))
        if y not in seen:
            seen.append(y)
    return seen


def memory_year(ts_ms: int | None) -> int | None:
    if ts_ms is None:
        return None
    return time.gmtime(ts_ms / 1000.0).tm_year


def apply_year_filter(scored: list, years: list[int], exempt: set[str]) -> list:
    """按年份过滤；RULE/no_decay 及显式豁免 id 不过滤。scored 元素 (mem, score)。"""
    if not years:
        return scored
    ys = set(years)
    out = []
    for mem, score in scored:
        if mem.id in exempt or mem.mem_type.value == "RULE" or mem.no_decay:
            out.append((mem, score))
            continue
        ts = mem.event_ts_ms if mem.event_ts_ms is not None else mem.created_ts_ms
        my = memory_year(ts)
        if my is not None and my in ys:
            out.append((mem, score))
    return out
