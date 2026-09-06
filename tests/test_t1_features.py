"""T1 特性单测：H1 会话级 RRF / J1 确定性规划 / F2 时间锚定（2026-09-06）。"""

from __future__ import annotations

from cortexa.models import MemoryInstance
from cortexa.modules.query_plan import parse_query_plan
from cortexa.modules.session_rrf import session_rrf_bonus
from cortexa.modules.time_anchor import parse_relative_time


def _mem(mid: str, session: str | None, score: float) -> tuple[str, tuple[MemoryInstance, float]]:
    m = MemoryInstance(id=mid, scope="s", session_id=session, content=mid)
    return mid, (m, score)


def test_h1_session_bonus_aggregates_and_orders() -> None:
    hits = dict([
        _mem("a1", "sA", 0.6), _mem("a2", "sA", 0.5),
        _mem("b1", "sB", 0.9), _mem("b2", "sB", 0.05),
    ])
    bonus = session_rrf_bonus(hits, k=60, weight=1.0)
    # sB 总分 0.95 > sA 1.1? sA=1.1, sB=0.95 → sA rank1 拿更大加成
    assert bonus["a1"] > bonus["b1"]
    assert bonus["a1"] == bonus["a2"]  # 同会话同加成
    assert 0 < bonus["b2"]  # 次会话也有正加成（保留广度）


def test_j1_deterministic_plan_fields() -> None:
    plan = parse_query_plan("How many months ago did I attend the photography workshop in Berlin?")
    assert isinstance(plan["core_terms"], list) and "months" in plan["core_terms"]
    assert "Berlin" in plan["entities"]
    assert plan["evidence_needs"] == []
    # 零 LLM 前提：plan 里没有模型产物字段
    assert plan["intent"] == "recall"
    # 时间线索仅在数值/绝对形式出现（many months ago 为词形，确定性不猜）
    plan2 = parse_query_plan("Which month in 2023 did I buy the car?")
    assert any("2023" in c for c in plan2["temporal_cues"]), plan2["temporal_cues"]


def test_f2_relative_time_anchor() -> None:
    ref = 1_700_000_000_000
    out = parse_relative_time("I moved here 2 weeks ago, on 2023-05-08.", ref)
    cues = [c for _t, c in out]
    assert any(c.startswith("rel:2 weeks ago") for c in cues)
    assert any(c.startswith("abs:2023-05-08") for c in cues)
    # 两周围绕 ref 的绝对差 ≈ 14 天
    two_weeks = next(t for t, c in out if c.startswith("rel:2 weeks ago"))
    assert abs((ref - two_weeks) - 14 * 86_400_000) < 1000

    cn = parse_relative_time("上个月搬的家，去年离职。", ref)
    assert any("rel:上个月" in c for _t, c in cn)
    assert any("rel:去年" in c for _t, c in cn)


def test_f2_calendar_arithmetic_month_leap() -> None:
    """v3 复核（2026-09-06）：月/年按真实日历回溯——月底与闰日裁剪，非 30/365 天近似。"""
    from datetime import datetime, timezone

    def utc_date(ms: int):
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).date()

    def ref_of(y: int, mo: int, d: int) -> int:
        return int(datetime(y, mo, d, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)

    # 月底裁剪：05-31 减 1 月 → 04-30（30 天近似会错成 05-01）
    o = parse_relative_time("1 month ago", ref_of(2026, 5, 31))
    ts = next(t for t, c in o if c.startswith("rel:1 month ago"))
    d0 = utc_date(ts)
    assert (d0.year, d0.month, d0.day) == (2026, 4, 30), d0

    # 闰日裁剪：2024-02-29 last year → 2023-02-28
    o = parse_relative_time("last year", ref_of(2024, 2, 29))
    ts = next(t for t, c in o if c.startswith("rel:last year"))
    d0 = utc_date(ts)
    assert (d0.year, d0.month, d0.day) == (2023, 2, 28), d0

    # 非闰 2 月裁剪：2026-03-31 上个月 → 2026-02-28
    o = parse_relative_time("上个月", ref_of(2026, 3, 31))
    ts = next(t for t, c in o if c.startswith("rel:上个月"))
    d0 = utc_date(ts)
    assert (d0.year, d0.month, d0.day) == (2026, 2, 28), d0

    # 天/周语义不变：仍是 24h 减法
    o = parse_relative_time("2 weeks ago", ref_of(2026, 5, 31))
    ts = next(t for t, c in o if c.startswith("rel:2 weeks ago"))
    assert abs((ref_of(2026, 5, 31) - ts) - 14 * 86_400_000) < 1000


def test_f2_relative_window_calendar_bounds() -> None:
    """temporal_conflict_eval_v1：相对时间窗（F2 检索侧消费）日历边界正确。"""
    from datetime import datetime, timezone

    def date_of(ms: int):
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).date()

    def ref(y, mo, d): return int(datetime(y, mo, d, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)

    from cortexa.modules.time_anchor import relative_cue, relative_window

    # last month：now=2026-04-10 → 2026-03 整月（30 天近似 03-11 起会错）
    w0, w1 = relative_window(ref(2026, 4, 10), "month", 1)
    assert (date_of(w0).year, date_of(w0).month, date_of(w0).day) == (2026, 3, 1)
    assert (date_of(w1).year, date_of(w1).month, date_of(w1).day) == (2026, 3, 31)

    # 闰月：last month now=2024-04-30 → 2024-03（02-29 事件不在内）
    w0, w1 = relative_window(ref(2024, 4, 30), "month", 1)
    assert date_of(w0).month == 3 and date_of(w1).month == 3

    # 跨年：last year now=2026-01-05 → 2025-01-01..2025-12-31
    w0, w1 = relative_window(ref(2026, 1, 5), "year", 1)
    assert (date_of(w0).year, date_of(w0).month, date_of(w0).day) == (2025, 1, 1)
    assert date_of(w1).year == 2025 and date_of(w1).month == 12

    # N months ago 数值形态：3 months ago now=2026-04-10 → 2026-01
    w0, w1 = relative_window(ref(2026, 4, 10), "month", 3)
    assert date_of(w0).month == 1 and date_of(w1).month == 1

    # 线索提取：只认明确相对表达，不带年份的普通问句不猜
    assert relative_cue("Who did Alice meet last month?") == ("month", 1)
    assert relative_cue("What did Alice do last year?") == ("year", 1)
    assert relative_cue("Where does Alice currently live?") is None  # 无相对线索不动