"""小说场景 · settle 结算落库（novel_lifecycle_apply.py）

输入：novel_lifecycle_settle.py 产出的 lifecycle_settle_latest.json（权威状态判定）；
输出：把结算事件链写入真实记忆库的 entity_states / lifecycle_events。

背景（2026-09-04 打通）：DB 记忆片段流（backfill 数据源）缺完整语境，死亡句常不在
抽取记忆内——entity_states 落库后 0 个 EXITED；而 settle 扫完整章节文件 + NARRATION
语境分层，权威判定赵铁柱(ch16)/王宣(ch18)/方桐(ch162) 死亡 EXITED。本工具把 settle
的权威结论同步进 DB，让 LifecycleGuard（检索时 EXITED 预警）有真实数据可用。

职责边界：backfill = 出场注册（entity_states 主名注册面）；
             settle = 状态判定（章节全文权威）；
             apply = 结算结果落库（事件重放，幂等）。三者互补不重叠。

幂等：lifecycle_events 按 (scope, entity_name, chapter, old_status, new_status) 去重；
      entity_states 按事件链终态 UPSERT。重复运行安全。

用法：python examples/novel_lifecycle_apply.py <小说根目录> [--json <settle json>]

退出码：0=完成；1=参数错误/无库/无报告。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cortexa.config import Settings
from cortexa.models import EntityType, LifecycleEvent, LifecycleStatus
from cortexa.storage import SQLiteMemoryStore

# 事件 -> DB 状态（settle 已用 LifecycleEngine 校验过转换合法性，此处直接映射）
_EV_TO_STATUS = {
    "death": LifecycleStatus.EXITED,
    "depart": LifecycleStatus.DORMANT,
    "return_": LifecycleStatus.REINTRODUCED,
    "transform": LifecycleStatus.TRANSFORMED,
}


def _evidences(ev: str, ch: int, note: str, gap: int) -> str:
    """把规则化结算转为 evidence 文本（LifecycleEvent 红线：必须有可追溯说明）。"""
    if ev == "death":
        return f"第{ch}章 NARRATION 语境死亡判定——后续出现视为引用"
    if ev == "depart":
        return f"第{ch}章后空白{gap}章（阈值外）自动离场"
    if ev == "return_":
        return f"第{ch}章再现（空白{gap}章）无交代回归"
    return note or f"第{ch}章状态转换"


def main() -> int:
    ap = argparse.ArgumentParser(description="settle 结算结果落库（事件重放）")
    ap.add_argument("root", type=Path, help="小说项目根目录（含 .cortexa/cortexa.db）")
    ap.add_argument("--json", type=Path, default=None,
                    help="settle 报告 json（默认 <root>/cortexa-out/lifecycle_settle_latest.json）")
    args = ap.parse_args()

    db_path = args.root / ".cortexa" / "cortexa.db"
    if not db_path.exists():
        print(f"未找到记忆库: {db_path}", file=sys.stderr)
        return 1

    report_path = args.json or (args.root / "cortexa-out" / "lifecycle_settle_latest.json")
    if not report_path.exists():
        print(f"未找到 settle 报告: {report_path}（先跑 novel_lifecycle_settle.py）",
              file=sys.stderr)
        return 1
    report = json.loads(report_path.read_text(encoding="utf-8"))

    scope = args.root.name
    settings = Settings(embedding_provider="hash", _env_file=None)
    store = SQLiteMemoryStore(str(db_path), embedding_dim=settings.embedding_dim)
    now_ms = int(datetime.now().timestamp() * 1000)

    events_written = 0
    states_updated = 0

    for item in report:
        name = item["name"]
        events = sorted(item.get("events", []), key=lambda e: e["ch"])  # 按章排序（方桐 death 后置）
        if not events:
            continue

        # 事件链终态 = 最后一事件的目标状态
        final_ev = events[-1]
        final_status = _EV_TO_STATUS.get(final_ev["ev"])
        if final_status is None:
            continue

        # 1) lifecycle_events 幂等写入（全链审计）
        for e in events:
            status = _EV_TO_STATUS.get(e["ev"])
            if status is None:
                continue
            dup = store.connection.execute(
                "SELECT 1 FROM lifecycle_events WHERE scope=? AND entity_name=? "
                "AND chapter=? AND new_status=?",
                (scope, name, e["ch"], status.value),
            ).fetchone()
            if dup:
                continue
            ev_obj = LifecycleEvent(
                entity_name=name,
                entity_type=EntityType.CHARACTER,
                old_status=None,  # settle 报告不含逐事件旧态；审计以链序表达
                new_status=status,
                chapter=e["ch"],
                evidence=_evidences(e["ev"], e["ch"], e.get("note", ""), e.get("gap", 0)),
                detail={"gap": e.get("gap", 0), "note": e.get("note", "")},
                ts_ms=now_ms,
            )
            with store.lock:
                store.connection.execute(
                    "INSERT INTO lifecycle_events "
                    "(scope, entity_name, entity_type, old_status, new_status, chapter, "
                    "evidence, detail_json, ts_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                    (scope, ev_obj.entity_name, ev_obj.entity_type.value,
                     ev_obj.old_status.value if ev_obj.old_status else None,
                     ev_obj.new_status.value, ev_obj.chapter, ev_obj.evidence,
                     json.dumps(ev_obj.detail, ensure_ascii=False), ev_obj.ts_ms),
                )
                store.connection.commit()
            events_written += 1

        # 2) entity_states：终态 UPSERT（first/last/appear 用 settle 权威章节值）
        with store.lock:
            existing = store.connection.execute(
                "SELECT 1 FROM entity_states WHERE scope=? AND entity_name=?",
                (scope, name),
            ).fetchone()
            if existing:
                store.connection.execute(
                    "UPDATE entity_states SET status=?, last_chapter=?, "
                    "appearance_count=?, detail_json=?, updated_ts_ms=? "
                    "WHERE scope=? AND entity_name=?",
                    (final_status.value, item.get("last", 0),
                     item.get("appear", 0),
                     json.dumps({"settle_chapter": final_ev["ch"],
                                 "settle_note": final_ev.get("note", "")},
                                ensure_ascii=False),
                     now_ms, scope, name),
                )
            else:
                store.connection.execute(
                    "INSERT INTO entity_states "
                    "(scope, entity_name, entity_type, status, first_chapter, "
                    "last_chapter, appearance_count, detail_json, updated_ts_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (scope, name, EntityType.CHARACTER.value, final_status.value,
                     item.get("first", 0), item.get("last", 0),
                     item.get("appear", 0),
                     json.dumps({"settle_chapter": final_ev["ch"]}, ensure_ascii=False),
                     now_ms),
                )
            store.connection.commit()
        states_updated += 1

    # 汇总
    dist = store.connection.execute(
        "SELECT status, COUNT(*) n FROM entity_states WHERE scope=? GROUP BY status",
        (scope,),
    ).fetchall()
    print("=== settle 落库完成 ===")
    print(f"事件写入: {events_written}（幂等去重后）")
    print(f"实体状态更新: {states_updated}")
    print(f"entity_states 分布: { {r[0]: r[1] for r in dist} }")
    exited = [r[0] for r in store.connection.execute(
        "SELECT entity_name FROM entity_states WHERE scope=? AND status='EXITED'",
        (scope,),
    ).fetchall()]
    if exited:
        print(f"EXITED 实体（LifecycleGuard 将预警）: {exited}")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
