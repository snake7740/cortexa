"""T3.1 stress_smoke：平台形态压测（2026-09-06，只观测不设门）。

模拟官方逐 chunk Add(500 chunk x 4 消息 = 2000 条)与 Search 交替，记录 add/search
p50/p95 与失败计数。scene=aml/journal=eval 与参赛口径一致；hash 离线确定性。
用法：python examples/stress_smoke.py [--chunks 300]
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from cortexa.config import Settings  # noqa: E402
from cortexa.server import create_app  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="stress_smoke")
    ap.add_argument("--chunks", type=int, default=500)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="stress-") as td:
        settings = Settings(data_dir=Path(td), embedding_provider="hash",
                            scene="aml", journal_mode="eval", _env_file=None)
        add_t: list[float] = []
        srch_t: list[float] = []
        with TestClient(create_app(settings)) as client:
            for i in range(args.chunks):
                base = 1_700_000_000_000
                msgs = [
                    {"role": "user", "content": "fact a" + str(i) + " alpha beta gamma delta epsilon",
                     "timestamp": base + i * 1000},
                    {"role": "assistant", "content": "note b" + str(i) + " about alpha details numbered " + str(i),
                     "timestamp": base + 1000 + i * 1000},
                    {"role": "user", "content": "preference c" + str(i) + " prefers beta over gamma",
                     "timestamp": base + 2000 + i * 1000},
                    {"role": "assistant", "content": "rule r" + str(i) + " never use delta on epsilon",
                     "timestamp": base + 3000 + i * 1000},
                ]
                t0 = time.perf_counter()
                resp = client.post("/add", json={
                    "request_id": "s" + str(i), "scope": "u",
                    "session_id": "chunk" + str(i), "messages": msgs})
                resp.raise_for_status()
                add_t.append((time.perf_counter() - t0) * 1000)
                if i % 10 == 0:
                    t1 = time.perf_counter()
                    s = client.post("/search", json={
                        "query": "alpha " + str(i), "scope": "u", "top_k": 5})
                    s.raise_for_status()
                    srch_t.append((time.perf_counter() - t1) * 1000)

        def p95(xs: list[float]) -> float:
            return sorted(xs)[int(0.95 * (len(xs) - 1))] if xs else 0.0

        print("[stress] chunks=" + str(args.chunks) + " adds_ok=" + str(len(add_t))
              + " searches=" + str(len(srch_t)))
        print("[stress] add p50/p95 = " + str(round(statistics.median(add_t), 1))
              + "/" + str(round(p95(add_t), 1)) + " ms")
        if srch_t:
            print("[stress] search p50/p95 = " + str(round(statistics.median(srch_t), 1))
                  + "/" + str(round(p95(srch_t), 1)) + " ms")


if __name__ == "__main__":
    main()
