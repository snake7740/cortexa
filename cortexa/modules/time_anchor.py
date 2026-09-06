"""T1.2 F2：相对时间→绝对时间解析器（AMI/CHM 写入层时间锚定方向，2026-09-06）。

给定消息文本与参考时刻(ref_ms，通常为会话最新消息时间戳)，把常见相对时间
表达式解析为绝对毫秒时间戳。近似口径：月≈30 天、年≈365 天（不引入日历库）。
纯函数可单测；写入期接线待含时间戳的官方形态数据验证（content 不改，锚定值
供 event_ts/时间过滤消费）。
"""

from __future__ import annotations

import re

_DAY = 86_400_000

_EN_REL = {
    "yesterday": 1, "前天": 2, "last week": 7, "上周": 7, "昨天": 1,
}
_CN_MONTH = {"上个月": 1, "last month": 1}
_CN_YEAR = {"去年": 1, "last year": 1}

_NUM = re.compile(r"(\d+)\s+(day|days|天|week|weeks|周|month|months|个月|year|years|年)\s+ago", re.IGNORECASE)
_DATE = re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def parse_relative_time(text: str, ref_ms: int) -> list[tuple[int, str]]:
    """解析文本中的相对/绝对时间表达，返回 [(绝对ms, 命中原语)] 降序。"""
    low = (text or "").lower()
    out: list[tuple[int, str]] = []
    for word, days in {**_EN_REL}.items():
        if word in low:
            out.append((ref_ms - days * _DAY, "rel:" + word))
    for word, months in _CN_MONTH.items():
        if word in low:
            out.append((ref_ms - months * 30 * _DAY, "rel:" + word))
    for word, years in _CN_YEAR.items():
        if word in low:
            out.append((ref_ms - years * 365 * _DAY, "rel:" + word))
    for m in _NUM.finditer(low):
        n = int(m.group(1))
        unit = m.group(2)
        days = n * {"day": 1, "days": 1, "天": 1, "week": 7, "weeks": 7, "周": 7,
                    "month": 30, "months": 30, "个月": 30, "year": 365,
                    "years": 365, "年": 365}[unit]
        out.append((ref_ms - days * _DAY, "rel:" + m.group(0).strip()))
    for m in _DATE.finditer(text):
        import calendar
        y, mo, d = (int(m.group(i)) for i in (1, 2, 3))
        try:
            ts = calendar.timegm((y, mo, d, 0, 0, 0)) * 1000
            out.append((ts, "abs:" + m.group(0)))
        except ValueError:
            pass
    seen: set[int] = set()
    uniq = []
    for ts, cue in sorted(out, key=lambda x: -x[0]):
        if ts not in seen:
            seen.add(ts)
            uniq.append((ts, cue))
    return uniq