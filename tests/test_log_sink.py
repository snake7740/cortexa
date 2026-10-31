"""日志面回归：cortexa.* 的 INFO 必须真的出现在进程输出里（docker logs 同源）。

2026-10-31 取证：uvicorn 只配置 `uvicorn.*` logger，根 logger 无 handler，
`logging.lastResort` 只放行 WARNING+，导致 HOTPATH 时间序列与护栏自报两条 INFO
全程静默——线上表现为 `docker logs | grep HOTPATH` 零命中。本测试锁死这个回归。
"""

from __future__ import annotations

import io
import logging

from cortexa.server import create_app


def test_info_from_cortexa_namespace_is_emitted():
    """不依赖 capsys（pytest 会接管流，拿不到 handler 写入的 stream）。"""
    create_app()  # 工厂内挂 sink，幂等
    lg = logging.getLogger("cortexa")
    sinks = [h for h in lg.handlers if getattr(h, "_cortexa_sink", False)]
    assert len(sinks) == 1, f"sink 数量应为 1，实为 {len(sinks)}"
    assert lg.level == logging.INFO, "命名空间级别未放开到 INFO，lastResort 仍会丢弃"
    buf = io.StringIO()
    saved = sinks[0].stream
    sinks[0].stream = buf
    try:
        logging.getLogger("cortexa.server").info("HOTPATH probe-line")
    finally:
        sinks[0].stream = saved
    assert "HOTPATH probe-line" in buf.getvalue()


def test_sink_not_duplicated_on_repeated_create_app():
    create_app()
    create_app()
    lg = logging.getLogger("cortexa")
    sinks = [h for h in lg.handlers if getattr(h, "_cortexa_sink", False)]
    assert len(sinks) == 1, f"sink 叠加 {len(sinks)} 个，日志会成倍重复"
