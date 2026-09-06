"""R1：年龄衰减排序因子（2026-09-06，纯函数）。
factor = 1 / (1 + age_days / half_days)；age 取 event_ts（缺省 created_ts）。
RULE/no_decay 豁免在调用侧处理（永不衰减红线）。默认关（settings.time_decay_enabled）。
"""
from __future__ import annotations

import time


def age_factor(ts_ms: int | None, now_ms: int | None = None,
               half_days: float = 180.0) -> float:
    if ts_ms is None:
        return 1.0
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    age_days = max(0.0, (now - ts_ms) / 86_400_000.0)
    return 1.0 / (1.0 + age_days / max(half_days, 1.0))
