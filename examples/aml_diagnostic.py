"""AML 七维诊断预演（2026-09-04，报名周前自评工具）。

目标：9/20 报名周前把七维的"本地可测面"跑一遍，产出对照表——官方基线分 vs
本地近似 vs 机制状态（已落地/缺口）。与 local_eval 分工：
  local_eval = 检索回归裁判（改动前后跑同一题集看是否回退）；
  本工具     = 七维盘点快照（当前各维能打多少、缺什么机制）。

覆盖与诚实边界：
  A explicit facts   -> local_eval ANSWERABLE hit@1/MRR（精确内容匹配）
  B multi-hop        -> local_eval dim_B（线索齐备代理，非推理本身）
  C time/sequence    -> local_eval dim_C（时序召回）
  D governance       -> local_eval gov_supersede（状态级 supersede 验证）
  E personalization  -> 本工具新增：偏好类记忆在"个人问题"下的召回（PREFERENCE
                        mem_type 判定 + 精确匹配）。官方 E 需要长期画像一致性，
                        本地无多会话画像，只测单会话偏好召回面。
  F rule-execution   -> local_eval dim_F（规则稳定可见）
  G epistemic safety -> 本工具新增：不可答问题返回 0 条的比例（precision proxy）。
                        官方 G 是"不确定时拒答/标注置信"，需要 LLM 判答——本地
                        只测检索侧不污染（不返回噪声记忆），机制缺口已标注。

用法：python examples/aml_diagnostic.py
输出：七维对照表（stdout）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # import local_eval

import local_eval  # noqa: E402  (复用 SESSIONS/ANSWERABLE/run_eval 定义)

from fastapi.testclient import TestClient  # noqa: E402
from cortexa.config import Settings  # noqa: E402
from cortexa.server import create_app  # noqa: E402


def _run_e_personalization(data_dir: Path) -> tuple[int, int]:
    """E 最小测：偏好记忆被"个人化问题"精确召回。"""
    settings = Settings(data_dir=data_dir, embedding_provider="hash",
                        scene="aml", _env_file=None)
    stmts = [
        "Alice prefers quiet workspaces for deep work",
        "Alice prefers tea over coffee",
        "Bob prefers standing desks",
    ]
    queries = [
        "what kind of workspace does Alice prefer",
        "what does Alice prefer to drink",
        "what desk does Bob prefer",
    ]
    golds = stmts
    passed = 0
    with TestClient(create_app(settings)) as client:
        client.post("/add", json={
            "request_id": "e1", "scope": "pref", "session_id": "e1",
            "messages": [{"role": "user", "content": s, "timestamp": 1_700_000_000_000 + i * 1000}
                         for i, s in enumerate(stmts)],
        })
        for q, g in zip(queries, golds):
            r = client.post("/search", json={"query": q, "scope": "pref", "top_k": 5})
            hits = [h["content"] for h in r.json()["data"]]
            if any(g in h for h in hits):
                passed += 1
    return passed, len(queries)


def _run_g_epistemic(data_dir: Path) -> tuple[int, int]:
    """G 最小测（precision proxy）：不可答问题应返回 0 条。

    官方 G 的"拒答"是平台用 LLM 基于返回记忆判断——本地只锁检索侧不污染。
    对已灌入一组事实的 scope 提无关问题：返回 >0 即 precision 风险信号。
    """
    settings = Settings(data_dir=data_dir, embedding_provider="hash",
                        scene="aml", _env_file=None)
    facts = [
        "The database migration finished on March 3rd",
        "Staging environment uses hash embedding provider",
        "Monthly cost review happens on the first Monday",
    ]
    unanswerable = [
        "What is the CEO's favorite color",
        "How many employees work at the company",
        "Where was the intern educated",
    ]
    empty = 0
    with TestClient(create_app(settings)) as client:
        client.post("/add", json={
            "request_id": "g1", "scope": "epi", "session_id": "g1",
            "messages": [{"role": "user", "content": s, "timestamp": 1_700_000_000_000 + i * 1000}
                         for i, s in enumerate(facts)],
        })
        for q in unanswerable:
            r = client.post("/search", json={"query": q, "scope": "epi", "top_k": 5})
            if len(r.json()["data"]) == 0:
                empty += 1
    return empty, len(unanswerable)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cortexa-diag-") as td:
        base = Path(td)

        # A/B/C/D/F 复用 local_eval（entity_channel=on 档）
        rep = local_eval.run_eval(data_dir=base / "eval", entity_channel=True, top_k=5)
        s = rep.summary()

        # E/G 新增
        e_pass, e_total = _run_e_personalization(base / "e")
        g_empty, g_total = _run_g_epistemic(base / "g")

    print()
    print("=== AML 七维诊断预演（本地近似，9/20 前自评）===")
    print("维度 | 官方基线参考 | 本地近似 | 机制状态")
    print("------|------------|---------|--------")
    rows = [
        ("A explicit facts", "89.9 (champion)", f"hit@1={s['hit@1']} mrr={s['mrr']}",
         "双通道+FSRS 已落地"),
        ("B multi-hop", "63.4", f"线索齐备 {s['dim_B']}",
         "检索侧代理；推理依赖平台；P4 关联传播缺口"),
        ("C time/sequence", "60.0", f"时序召回 {s['dim_C']}",
         "治理已接；RRF 时间衰减因子缺口(P4)"),
        ("D governance", "51.2", f"supersede {s['gov_supersede']} / 防过度 {s['gov_no_overkill']}",
         "状态级 supersede 已落地+防误伤"),
        ("E personalization", "-", f"{e_pass}/{e_total} 偏好召回",
         "单会话偏好可召回；长期画像一致性缺口(P4 贝叶斯)"),
        ("F rule-execution", "30.0", f"规则可见 {s['dim_F']}",
         "RULE wing no_decay+boost 已落地"),
        ("G epistemic safety", "-", f"{g_empty}/{g_total} 空返回",
         "检索侧 precision 已守；LLM 拒答判答缺口(P4 confidence)"),
    ]
    for dim, off, local, mech in rows:
        print(f"{dim:18} | {off:15} | {local:22} | {mech}")
    print()
    print("注：官方基线为历届参考；本地近似是离线确定性面（hash 嵌入无语义），")
    print("    B/E/G 的推理/画像/拒答判答需平台 LLM——本地只锁检索侧不变量。")
    return 0


if __name__ == "__main__":
    sys.exit(main())