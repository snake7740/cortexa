"""Q8 evidence packaging 纯函数测试（2026-09-06）。

不变量：membership/order 不变，只变槽内文本；raw 档零变化。
"""
from __future__ import annotations

from cortexa.models import MemoryInstance
from cortexa.modules.evidence_packaging import MODES, pack_question


def _mem(mid: str, content: str) -> MemoryInstance:
    return MemoryInstance(id=mid, scope="s", content=content)


def _timeline() -> tuple[list[MemoryInstance], list[str], dict]:
    order = ["a", "b", "c", "d"]
    contents = {
        "a": "A is born in 1990",
        "b": "A moved to Paris in 2020",
        "c": "A works at Google since 2022",
        "d": "A bought a car last month",
    }
    evs = [_mem(k, contents[k]) for k in order]
    return evs, order, contents.get  # content_of = dict.get


def test_raw_is_zero_change() -> None:
    evs, order, content_of = _timeline()
    texts, added = pack_question(evs, order, content_of, "raw")
    assert added == 0
    assert texts == [e.content for e in evs]  # membership/order/text 全不变


def test_pm1_appends_previous_neighbor() -> None:
    evs, order, content_of = _timeline()
    texts, added = pack_question([evs[2]], order, content_of, "pm1")
    # c 的紧邻前文是 b
    assert texts[0] == "A works at Google since 2022\nA moved to Paris in 2020"
    assert added == len("A moved to Paris in 2020")


def test_pm2_nearest_first_two() -> None:
    evs, order, content_of = _timeline()
    texts, _added = pack_question([evs[2]], order, content_of, "pm2")
    # 就近在前：b 先于 a
    assert texts[0] == (
        "A works at Google since 2022\n"
        "A moved to Paris in 2020\nA is born in 1990"
    )


def test_first_evidence_no_previous_context() -> None:
    evs, order, content_of = _timeline()
    texts, added = pack_question([evs[0]], order, content_of, "pm1")
    assert texts[0] == "A is born in 1990"  # 无前文邻接
    assert added == 0


def test_membership_and_order_invariant() -> None:
    evs, order, content_of = _timeline()
    # 返回序 [c, a] 固定，包装不重排
    texts, _a = pack_question([evs[2], evs[0]], order, content_of, "pm2")
    assert texts[0].startswith("A works at Google")
    assert texts[1] == "A is born in 1990"  # 首条证据无邻接，原样
    assert all(m in MODES for m in ("raw", "pm1", "pm2", "raw-atomic"))
