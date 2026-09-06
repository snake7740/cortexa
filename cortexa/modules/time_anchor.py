"""T1.2 F2：相对时间→绝对时间解析器（AMI/CHM 写入层时间锚定方向，2026-09-06）。

给定消息文本与参考时刻(ref_ms，通常为会话最新消息时间戳)，把常见相对时间
表达式解析为绝对毫秒时间戳。日历运算口径（v3 复核 2026-09-06）：月/年按真实日历
回溯（stdlib datetime+calendar，月底/闰日裁剪，不引入第三方库）；天/周仍是 24h 减法。
判官时间粒度严格——30/365 天近似会造成日级锚定偏移（2026-05-31 减一个月应为 04-30
而非 05-01）。纯函数可单测；写入期接线待含时间戳的官方形态数据验证（content 不改，
锚定值供 event_ts/时间过滤消费）。
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


def _shift_months(ref_ms: int, months_back: int) -> int:
    """真实日历月份回溯（月底裁剪：2026-05-31 减 1 月 → 2026-04-30）。"""
    import calendar
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ref_ms / 1000.0, tz=timezone.utc)
    y, m0 = divmod(dt.year * 12 + (dt.month - 1) - months_back, 12)
    m = m0 + 1
    day = min(dt.day, calendar.monthrange(y, m)[1])
    return int(datetime(y, m, day, dt.hour, dt.minute, dt.second,
                        tzinfo=timezone.utc).timestamp() * 1000)


def _shift_years(ref_ms: int, years_back: int) -> int:
    """真实日历年份回溯（闰日裁剪：2024-02-29 减 1 年 → 2023-02-28）。"""
    import calendar
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ref_ms / 1000.0, tz=timezone.utc)
    y = dt.year - years_back
    day = min(dt.day, calendar.monthrange(y, dt.month)[1])
    return int(datetime(y, dt.month, day, dt.hour, dt.minute, dt.second,
                        tzinfo=timezone.utc).timestamp() * 1000)


# ---- F2 检索侧消费（2026-09-06，temporal_conflict_eval_v1）----
# 相对时间过滤窗口（日历）：month/year 整单位日历区间，day/week 按自然日。
# 判官粒度严格且 C 轴是检索派结构窗——30/365 天近似在跨月/闰年/月底题上错位，
# 本函数用 stdlib 日历（与 _shift_months/_shift_years 同源）产出正确窗口。

_MONTH_CUE = re.compile(r"(\b(?:last month|上个月)\b|(\d+)\s+months?\s+ago)", re.IGNORECASE)
_YEAR_CUE = re.compile(r"(\b(?:last year|去年)\b|(\d+)\s+years?\s+ago)", re.IGNORECASE)
_DAY_CUE = re.compile(r"(\b(?:yesterday|昨天|last week|上周)\b|(\d+)\s+days?\s+ago)", re.IGNORECASE)


def relative_window(ref_ms: int, unit: str, n_back: int = 1) -> tuple[int, int]:
    """返回第 n_back 个完整单位的日历窗口 [start_ms, end_ms]（UTC 自然边界）。

    unit: month=第 n_back 个日历月；year=第 n_back 个日历年；
          day=第 n_back 个自然日（昨天=n_back=1）；week=近 7 个自然日。
    例：ref=2026-03-31, unit=month, n_back=1 -> 2026-02-01 00:00 ~ 2026-02-28 23:59:59.999
        （30 天近似会错成 03-01 起——跨月边界题即裁决点）
    """
    import calendar
    from datetime import datetime, timedelta, timezone
    dt = datetime.fromtimestamp(ref_ms / 1000.0, tz=timezone.utc)
    if unit == "month":
        y, m0 = divmod(dt.year * 12 + (dt.month - 1) - n_back, 12)
        m = m0 + 1
        start = datetime(y, m, 1, tzinfo=timezone.utc)
        last = calendar.monthrange(y, m)[1]
        end = datetime(y, m, last, 23, 59, 59, 999000, tzinfo=timezone.utc)
    elif unit == "year":
        y = dt.year - n_back
        start = datetime(y, 1, 1, tzinfo=timezone.utc)
        end = datetime(y, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc)
    elif unit == "day":
        d0 = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        start = d0 - timedelta(days=n_back)
        end = d0 - timedelta(days=n_back - 1, milliseconds=1)
    elif unit == "week":
        d0 = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        start = d0 - timedelta(days=7)
        end = d0 - timedelta(milliseconds=1)
    else:
        raise ValueError(f"unknown unit: {unit}")
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def relative_cue(query: str) -> tuple[str, int] | None:
    """从查询提取单个相对时间线索。返回 (unit, n_back)；无则 None（不猜）。"""
    low = (query or "").lower()
    m = _MONTH_CUE.search(low)
    if m:
        return ("month", int(m.group(2)) if m.group(2) else 1)
    m = _YEAR_CUE.search(low)
    if m:
        return ("year", int(m.group(2)) if m.group(2) else 1)
    m = _DAY_CUE.search(low)
    if m:
        g = m.group(1) or ""
        if "week" in g or "周" in g:
            return ("week", 1)
        return ("day", int(m.group(2)) if m.group(2) else 1)
    return None


def _mem_ts(mem) -> int | None:
    return mem.event_ts_ms if mem.event_ts_ms is not None else mem.created_ts_ms


def apply_relative_window(scored: list, query: str, ref_ms: int) -> list:
    """相对时间窗过滤（F2 检索侧；默认关 settings.time_anchor_enabled）。

    RULE/no_decay 豁免（同 R4 口径）。无相对线索 = 原样返回（确定性零变化）。
    """
    cue = relative_cue(query)
    if cue is None:
        return scored
    w0, w1 = relative_window(ref_ms, cue[0], cue[1])
    out = []
    for mem, score in scored:
        if mem.mem_type.value == "RULE" or mem.no_decay:
            out.append((mem, score))
            continue
        ts = _mem_ts(mem)
        if ts is None:
            out.append((mem, score))
            continue
        if w0 <= ts <= w1:
            out.append((mem, score))
    return out


def parse_relative_time(text: str, ref_ms: int) -> list[tuple[int, str]]:
    """解析文本中的相对/绝对时间表达，返回 [(绝对ms, 命中原语)] 降序。"""
    low = (text or "").lower()
    out: list[tuple[int, str]] = []
    for word, days in {**_EN_REL}.items():
        if word in low:
            out.append((ref_ms - days * _DAY, "rel:" + word))
    for word, months in _CN_MONTH.items():
        if word in low:
            out.append((_shift_months(ref_ms, months), "rel:" + word))
    for word, years in _CN_YEAR.items():
        if word in low:
            out.append((_shift_years(ref_ms, years), "rel:" + word))
    for m in _NUM.finditer(low):
        n = int(m.group(1))
        unit = m.group(2).lower()
        if unit in {"month", "months", "个月"}:
            out.append((_shift_months(ref_ms, n), "rel:" + m.group(0).strip()))
        elif unit in {"year", "years", "年"}:
            out.append((_shift_years(ref_ms, n), "rel:" + m.group(0).strip()))
        else:
            days = n * {"day": 1, "days": 1, "天": 1, "week": 7, "weeks": 7, "周": 7}[unit]
            out.append((ref_ms - days * _DAY, "rel:" + m.group(0).strip()))
    for m in _DATE.finditer(text):
        import calendar  # noqa: PLC0415 —— 仅绝对日期分支使用，延迟导入
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