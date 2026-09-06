"""本地评测模拟器（w3_simulator）：官方评测流程的离线近似回归。

业务目标：不依赖官方平台，在本地对"多轮会话 Add -> 问答 Search"全流程打分，
作为任何检索侧改动（通道增删/排序调权）的回归裁判——改动前后跑同一份内置
题集，Hit@k/MRR 不得回退。离线确定性（hash 嵌入，无网络），数秒出表。

契约保真：走真实 HTTP 契约路径（TestClient -> POST /add -> POST /search），
字段/同步语义/降序与官方一致；评测数据不写业务库（临时目录，跑完即弃）。

场景选择：本评测是 AML（比赛）评测模拟器——治理/增强/生命周期必须按 aml
schema 执行（scene="aml"）；否则治理拿 novel schema 的中文事件词治理英文偏好，
AML 维度 D 治理词（supersede/update/withdraw）全部空转（2026-09-04 实测漂移）。

消融：--ablation 对 entity_channel 开/关各跑一遍——通道去留由分数说话
（研究文档 2026-08-31：若伤精度，config 开关置 False 回双通道基线）。

用法：
    python examples/local_eval.py            # 默认 --ablation
    python examples/local_eval.py --top-k 5  # 只跑开档
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 自足：不依赖 cwd/PYTHONPATH

from fastapi.testclient import TestClient

from cortexa.config import Settings
from cortexa.server import create_app

_TS_BASE = 1_700_000_000  # 秒；逐会话递增，模拟真实对话时间流

# 内置会话（官方形态：多轮 messages 一次性 Add；事实风格贴近 LongMemEval 题型）
SESSIONS: list[list[str]] = [
    ["Alice moved to Sweden in 2019.",
     "Alice adopted a dog named Rob.",
     "Alice works at Zephyr Labs."],
    ["Never deploy to production on Friday afternoon.",
     "The team uses pnpm as the only package manager.",
     "Code reviews require two approvals."],
    ["叶长生在第十章突破到炼气七层。",
     "苏晚晴的信物是一枚黑色木牌。",
     "黑色木牌背面刻着残缺星图。"],
    ["The team offsite is scheduled for October 15.",
     "Bob's email is bob@zephyr.dev.",
     "Bob leads the platform team."],
    ["The database migration finished on March 3rd.",
     "Staging environment uses hash embedding provider.",
     "Monthly cost review happens on the first Monday."],
]

# 可回答题：gold = 入库记忆的原文（精确内容匹配计命中）
ANSWERABLE: list[tuple[str, str]] = [
    ("Where does Alice live now?", "Alice moved to Sweden in 2019."),
    ("What is the name of Alice's dog?", "Alice adopted a dog named Rob."),
    ("Where does Alice work?", "Alice works at Zephyr Labs."),
    ("When is it forbidden to deploy?", "Never deploy to production on Friday afternoon."),
    ("Which package manager does the team use?", "The team uses pnpm as the only package manager."),
    ("叶长生在什么时候突破炼气七层？", "叶长生在第十章突破到炼气七层。"),
    ("黑色木牌是谁的信物？", "苏晚晴的信物是一枚黑色木牌。"),
    ("黑色木牌背面有什么？", "黑色木牌背面刻着残缺星图。"),
    ("When is the team offsite?", "The team offsite is scheduled for October 15."),
    ("How can Bob be contacted?", "Bob's email is bob@zephyr.dev."),
    ("When did the database migration finish?", "The database migration finished on March 3rd."),
]

# 不可回答题：库中无对应事实。离线无法语义判答，只作观测指标（返回条数/头部
# 平均分），不设定 pass/fail——precision-first 的裁判权留给官方评测。
UNANSWERABLE: list[str] = [
    "Where did Alice go to university?",
    "孟知秋的师父是谁？",
    "What GPU model does the staging server have?",
]


# ---------------------------------------------------------------------------
# 维度 D 治理用例（2026-09-04 补：治理从"不干扰评测"升级为"被评测的维度"）
# 每条 = 同会话先旧后新两次陈述：治理正确时旧记忆被 supersede（状态级验证：
# 直连 db 查 status=SUPERSEDED + superseded_by 指向新 id），新记忆可被 Search。
# 独立 scope（gov-N）不污染上面 eval-local 检索基线。
# ---------------------------------------------------------------------------
GOVERNANCE_CASES: list[dict] = [
    # update 词 currently：新陈述取代旧偏好
    {"scope": "gov-1", "old": "Alice prefers tea over coffee",
     "new": "Alice currently prefers coffee",
     "query": "what drink does Alice prefer now"},
    # supersede 词 no longer：否定旧陈述
    {"scope": "gov-2", "old": "Bob uses pnpm for builds",
     "new": "Bob no longer uses pnpm",
     "query": "which package manager does Bob use"},
    # update 词 now + prefer over：偏好重排（具名主语保证治理可提取 subject）
    {"scope": "gov-3", "old": "Bob prefers Slack over Teams for chat",
     "new": "Bob now prefers Teams over Slack",
     "query": "which chat tool does Bob prefer"},
]
# 防过度对照：补充性新陈述（同 subject 不同属性）不得误杀旧记忆
GOVERNANCE_NEGATIVE: list[dict] = [
    {"scope": "gov-n1", "old": "Alice prefers tea over coffee",
     "new": "Alice adopted a dog named Rob",
     "query": "what is the name of Alice's pet"},
]


# ---------------------------------------------------------------------------
# 维度 B/C/F 评测用例（2026-09-04 补，AML 七维本地近似回归）
#   B multi-hop（诚实化）= 线索齐备：同实体多条记忆须全部被检索带回——平台以
#     top-K 拼答案，缺任一线索即无法推理；检索侧必要条件，非推理本身。
#   C time/sequence = 正确时间点/最新状态的事实可检索（治理 supersede 后旧消失
#     由维度 D 段覆盖，此处只测时序召回）。
#   F rule-execution = 规则记忆稳定可见（no_decay + rule_boost 的直接验证：
#     RULE 永不衰减、排序加成，问规则必能召回）。
# 独立 scope（dim-*），不污染 eval-local 检索基线。判定 = needed 记忆全部在
# top_k 内（词元确定性命中，非调参产物——检索改动致缺召回即回归）。
# ---------------------------------------------------------------------------
DIMENSION_CASES: list[dict] = [
    # ---- B：同实体线索齐备 ----
    {"dim": "B", "scope": "dim-b1",
     "stmts": ["Alice works at Zephyr Labs.",
               "Zephyr Labs is headquartered in Stockholm."],
     "query": "which city is Zephyr Labs located in",
     "needed": ["Alice works at Zephyr Labs.",
                "Zephyr Labs is headquartered in Stockholm."]},
    {"dim": "B", "scope": "dim-b2",
     "stmts": ["Alice adopted a dog named Rob.",
               "Rob is a golden retriever."],
     "query": "what kind of dog is Rob",
     "needed": ["Alice adopted a dog named Rob.",
                "Rob is a golden retriever."]},
    # ---- C：时间序列召回 ----
    {"dim": "C", "scope": "dim-c1",
     "stmts": ["Alice moved to Sweden in 2019.",
               "Alice moved to Norway in 2023."],
     "query": "where did Alice move in 2023",
     "needed": ["Alice moved to Norway in 2023."]},
    {"dim": "C", "scope": "dim-c2",
     "stmts": ["The database migration finished on March 3rd.",
               "A follow-up cleanup ran on March 10th."],
     "query": "when did the database migration finish",
     "needed": ["The database migration finished on March 3rd."]},
    # ---- F：规则稳定可见 ----
    {"dim": "F", "scope": "dim-f1",
     "stmts": ["Never deploy to production on Friday afternoon.",
               "The team uses pnpm."],
     "query": "when are deployments forbidden",
     "needed": ["Never deploy to production on Friday afternoon."]},
    {"dim": "F", "scope": "dim-f2",
     "stmts": ["You must get two approvals before merging to main.",
               "Bob likes hiking."],
     "query": "how many approvals are required before merging",
     "needed": ["You must get two approvals before merging to main."]},
]


@dataclass
class EvalReport:
    label: str
    n_answerable: int = 0
    hit1: int = 0
    hit5: int = 0
    mrr: float = 0.0
    unanswerable_returned: list[int] = field(default_factory=list)
    add_latency_ms: list[float] = field(default_factory=list)
    search_latency_ms: list[float] = field(default_factory=list)
    # ---- 维度 D 治理观测（2026-09-04：状态级 + 检索闭环双验证）----
    gov_total: int = 0
    gov_passed: int = 0
    gov_negative_total: int = 0
    gov_negative_passed: int = 0
    # ---- 维度 B/C/F 观测（2026-09-04：线索齐备/时序召回/规则可见）----
    dim_counts: dict[str, int] = field(default_factory=dict)  # dim -> total
    dim_passed: dict[str, int] = field(default_factory=dict)  # dim -> passed

    def summary(self) -> dict:
        n = max(self.n_answerable, 1)
        return {
            "label": self.label,
            "hit@1": round(self.hit1 / n, 3),
            "hit@5": round(self.hit5 / n, 3),
            "mrr": round(self.mrr / n, 3),
            "unanswerable_avg_results": round(
                statistics.mean(self.unanswerable_returned), 2
            ) if self.unanswerable_returned else 0.0,
            "add_p95_ms": round(sorted(self.add_latency_ms)[
                int(0.95 * (len(self.add_latency_ms) - 1))], 1)
                if self.add_latency_ms else 0.0,
            "search_p95_ms": round(sorted(self.search_latency_ms)[
                int(0.95 * (len(self.search_latency_ms) - 1))], 1)
                if self.search_latency_ms else 0.0,
            # 维度 D 治理（状态级 supersede 正确率；负向 = 防误杀对照）
            "gov_supersede": f"{self.gov_passed}/{self.gov_total}"
            if self.gov_total else "-",
            "gov_no_overkill": f"{self.gov_negative_passed}/{self.gov_negative_total}"
            if self.gov_negative_total else "-",
            # 维度 B/C/F（B=线索齐备 / C=时序召回 / F=规则可见）
            "dim_B": f"{self.dim_passed.get('B', 0)}/{self.dim_counts.get('B', 0)}"
            if self.dim_counts.get("B") else "-",
            "dim_C": f"{self.dim_passed.get('C', 0)}/{self.dim_counts.get('C', 0)}"
            if self.dim_counts.get("C") else "-",
            "dim_F": f"{self.dim_passed.get('F', 0)}/{self.dim_counts.get('F', 0)}"
            if self.dim_counts.get("F") else "-",
        }


def run_eval(*, data_dir: Path, entity_channel: bool, top_k: int = 5) -> EvalReport:
    """跑一遍完整评测：摄取全部会话 -> 可回答/不可回答题检索打分。"""
    settings = Settings(
        data_dir=data_dir, embedding_provider="hash",
        entity_channel=entity_channel,
        scene="aml",  # AML 评测：治理按 aml schema（防 novel/aml 漂移，见 docstring）
        journal_mode="eval",  # 与参赛部署同口径：日志脱敏 + 30 天 TTL 路径生效
        _env_file=None,
    )
    report = EvalReport(label=f"entity_channel={'on' if entity_channel else 'off'}")
    with TestClient(create_app(settings)) as client:
        ts = _TS_BASE
        for i, turns in enumerate(SESSIONS, start=1):
            t0 = time.perf_counter()
            resp = client.post("/add", json={
                "request_id": f"eval-s{i}", "scope": "eval-local",
                "session_id": f"eval-s{i}",
                "messages": [
                    {"role": "user", "content": c, "timestamp": (ts + j) * 1000}
                    for j, c in enumerate(turns)
                ],
            })
            resp.raise_for_status()
            report.add_latency_ms.append((time.perf_counter() - t0) * 1000)
            ts += 3600  # 每会话推进一小时（逻辑时间流）

        def _search(query: str) -> list[dict]:
            t0 = time.perf_counter()
            resp = client.post("/search", json={
                "query": query, "scope": "eval-local", "top_k": top_k,
            })
            resp.raise_for_status()
            report.search_latency_ms.append((time.perf_counter() - t0) * 1000)
            return resp.json()["data"]

        for query, gold in ANSWERABLE:
            report.n_answerable += 1
            results = _search(query)
            ranks = [i + 1 for i, r in enumerate(results) if r["content"] == gold]
            if ranks:
                if ranks[0] == 1:
                    report.hit1 += 1
                if ranks[0] <= 5:
                    report.hit5 += 1
                report.mrr += 1.0 / ranks[0]

        for query in UNANSWERABLE:
            report.unanswerable_returned.append(len(_search(query)))

        # ---- 维度 D 治理评测（2026-09-04）：状态级验证 + 检索闭环 ----
        # 治理在 Add 同步管线内已执行（governance_enabled=True, scene=aml）。
        # 判据是硬状态而非检索推测：直连 db 查旧记忆 status=SUPERSEDED 且
        # superseded_by 指向新记忆 id；再走 /search 验证新记忆可检索闭环。
        db_path = settings.data_dir / "cortexa.db"
        for case in GOVERNANCE_CASES:
            scope = case["scope"]
            ts = _TS_BASE + 10_000
            for rid, text in ((scope + "-old", case["old"]), (scope + "-new", case["new"])):
                _r = client.post("/add", json={
                    "request_id": rid, "scope": scope, "session_id": scope,
                    "messages": [{"role": "user", "content": text, "timestamp": ts * 1000}],
                })
                _r.raise_for_status()
                ts += 1
            # 状态级：旧应被 supersede
            con = sqlite3.connect(str(db_path))
            con.row_factory = sqlite3.Row
            try:
                old_row = con.execute(
                    "SELECT id, status, superseded_by FROM memories "
                    "WHERE scope = ? AND content = ?", (scope, case["old"])
                ).fetchone()
                new_row = con.execute(
                    "SELECT id FROM memories WHERE scope = ? AND content = ?",
                    (scope, case["new"])
                ).fetchone()
                ok_state = bool(
                    old_row and old_row["status"] == "SUPERSEDED"
                    and new_row and old_row["superseded_by"] == new_row["id"]
                )
            finally:
                con.close()
            # 检索闭环：新记忆应可被 Search 命中（top_k=5 内）
            _r = client.post("/search", json={
                "query": case["query"], "scope": scope, "top_k": 5})
            _r.raise_for_status()
            ok_search = any(case["new"] in h["content"] for h in _r.json()["data"])
            report.gov_total += 1
            if ok_state and ok_search:
                report.gov_passed += 1

        # ---- 防过度对照：补充性陈述不得误杀旧记忆 ----
        for case in GOVERNANCE_NEGATIVE:
            scope = case["scope"]
            ts = _TS_BASE + 20_000
            for rid, text in ((scope + "-old", case["old"]), (scope + "-new", case["new"])):
                _r = client.post("/add", json={
                    "request_id": rid, "scope": scope, "session_id": scope,
                    "messages": [{"role": "user", "content": text, "timestamp": ts * 1000}],
                })
                _r.raise_for_status()
                ts += 1
            con = sqlite3.connect(str(db_path))
            con.row_factory = sqlite3.Row
            try:
                old_row = con.execute(
                    "SELECT status FROM memories WHERE scope = ? AND content = ?",
                    (scope, case["old"])
                ).fetchone()
                ok_kept = bool(old_row and old_row["status"] == "ACTIVE")
            finally:
                con.close()
            report.gov_negative_total += 1
            if ok_kept:
                report.gov_negative_passed += 1

        # ---- 维度 B/C/F 评测（2026-09-04）：线索齐备 / 时序召回 / 规则可见 ----
        # 判据 = needed 记忆全部在 top_k 检索结果内（词元确定性命中）。独立 scope，
        # 与 eval-local 基线隔离；B 以"同实体多记忆齐备"作多跳检索侧近似。
        for case in DIMENSION_CASES:
            dim = case["dim"]
            scope = case["scope"]
            ts = _TS_BASE + 30_000
            for j, text in enumerate(case["stmts"]):
                _r = client.post("/add", json={
                    "request_id": f"{scope}-{j}", "scope": scope,
                    "session_id": scope,
                    "messages": [{"role": "user", "content": text,
                                  "timestamp": (ts + j) * 1000}],
                })
                _r.raise_for_status()
            _r = client.post("/search", json={
                "query": case["query"], "scope": scope, "top_k": 5})
            _r.raise_for_status()
            hit_texts = [h["content"] for h in _r.json()["data"]]
            ok = all(any(n in h for h in hit_texts) for n in case["needed"])
            report.dim_counts[dim] = report.dim_counts.get(dim, 0) + 1
            if ok:
                report.dim_passed[dim] = report.dim_passed.get(dim, 0) + 1
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Cortexa 本地评测模拟器（离线近似回归）")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--ablation", action="store_true", default=True,
                    help="entity_channel 开/关各跑一遍（默认开）")
    ap.add_argument("--no-ablation", dest="ablation", action="store_false")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="cortexa-eval-") as td:
        base = Path(td)
        reports = [run_eval(data_dir=base / "on", entity_channel=True, top_k=args.top_k)]
        if args.ablation:
            reports.append(run_eval(
                data_dir=base / "off", entity_channel=False, top_k=args.top_k
            ))

    print(f"\n=== Cortexa 本地评测模拟（{len(ANSWERABLE)} 可回答 + "
          f"{len(UNANSWERABLE)} 不可回答，top_k={args.top_k}） ===")
    header = ["label", "hit@1", "hit@5", "mrr",
              "unanswerable_avg_results", "add_p95_ms", "search_p95_ms",
              "gov_supersede", "gov_no_overkill", "dim_B", "dim_C", "dim_F"]
    print(" | ".join(header))
    for r in reports:
        s = r.summary()
        print(" | ".join(str(s[k]) for k in header))


if __name__ == "__main__":
    main()