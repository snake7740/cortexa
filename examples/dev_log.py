"""开发决策账本 CLI（本仓库 dogfooding：把「软件开发场景」从推断转实证）。

用法（数据落 ~/.cortexa，scope=cortexa-dev——与 facade 默认一致，日常随手可用）：
    python examples/dev_log.py remember "文本" [--tag 标签]
    python examples/dev_log.py recall "查询" [-k 3]
    python examples/dev_log.py retcon "旧文本子串" "新文本" [--reason 原因]
    python examples/dev_log.py audit
    python examples/dev_log.py stats

设计：
- 完全走 Cortexa 双入口管线（remember/recall/retcon/audit/journal），即真实使用；
- scope=cortexa-dev 固定（--scope 可覆盖），与小说/评测库隔离；
- retcon 走修正链留痕（旧转 SUPERSEDED + 继承时间轴）；
- audit 报同实体状态线/矛盾（决策口径修正时用）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 自足

from cortexa import Cortexa  # noqa: E402

DEFAULT_SCOPE = "cortexa-dev"


def _open(scope: str) -> Cortexa:
    return Cortexa()  # 默认 ~/.cortexa


def main() -> int:
    ap = argparse.ArgumentParser(description="开发决策账本（Cortexa dogfooding）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("remember")
    p.add_argument("text")
    p.add_argument("--tag", default="decision")
    p = sub.add_parser("recall")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=3)
    p = sub.add_parser("retcon")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--reason", default="口径修正")
    sub.add_parser("audit")
    sub.add_parser("stats")
    ap.add_argument("--scope", default=DEFAULT_SCOPE)
    a = ap.parse_args()

    with Cortexa() as cm:
        if a.cmd == "remember":
            r = cm.remember(a.text, scope=a.scope, role="user",
                            session_id="devlog", request_id=f"devlog-{a.tag}")
            print(f"remembered: stored={r.stored} duplicates={r.duplicates} scope={a.scope}")
            return 0
        if a.cmd == "recall":
            for item in cm.recall(a.query, scope=a.scope, top_k=a.k):
                print(f"- [{item.id[:8]}] {item.content[:160]}")
            return 0
        if a.cmd == "retcon":
            # 走 facade.retcon（修正链：旧转 SUPERSEDED + retcon 事件 + 时间轴继承）
            r = cm.retcon(a.scope, a.old, a.new, reason=a.reason)
            print(f"retconned: {r}")
            return 0
        if a.cmd == "audit":
            s = cm.journal.summary(a.scope)
            print(f"journal summary: {s}")
            return 0
        if a.cmd == "stats":
            print(cm.stats(a.scope))
            print("orphans:", cm.journal.orphan_stats(a.scope))
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())