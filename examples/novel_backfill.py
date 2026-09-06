"""存量回填：对历史章节重建 entity_states/lifecycle_events 的角色出场注册。

背景：entity_states/lifecycle_events 表在 novel_adapter 启用前为空——208 章记忆在适配器
上线前已摄取，从未跑过 register_new_entities + track_characters。本脚本从存量数据重建
两张表，让"下线角色复活"防护立刻有数据可用。

方法（确定性本地，零 LLM，不耗 API）：
1. 逐章（按 session_id chNNN）取活跃记忆，拼接为本章文本；
2. 角色在场检测 = 在章节文本中匹配 Cast 名单（schema.characters 主名 + aliases 别名），
   归一到主名——与 novel_lifecycle_settle.py 同源哲学（文本在场驱动，不发明实体）；
3. register_new_entities 注册（新实体 INTRODUCED，已有则更新出场计数/章节）；
4. track_characters 做确定性状态转换检测（仅当整章文本有明确转换关键词）。

职责边界（2026-09-04 重构后实证确立）：
- 本工具 = 出场注册 + 出场统计（entity_states 的主名注册面）。数据源是 DB 记忆片段，
  适合"谁在哪章出场"这类聚合，注册 22 个 schema 角色全绿。
- 死亡/离场等**状态判定**不由本工具负责——DB 片段流缺失完整语境（死亡句常不在抽取
  记忆内，且 chsettings 设定表含"已死"字样会污染判定）。精确状态结算请看
  novel_lifecycle_settle.py（扫完整章节文件 + NARRATION 语境分层）。两者互补不重叠。

幂等：已回填章节跳过（entity_states.last_chapter >= 该章）。全量重跑加 --force 先清表。

用法：
    python examples/novel_backfill.py <小说根目录> [--force] [--report]

退出码：0=完成；1=参数错误/无库。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cortexa.config import Settings
from cortexa.domain import CastRegistry
from cortexa.llm import LLMClient
from cortexa.models import (
    EntityType,
    MemoryInstance,
    MemoryType,
    ResolvedEntity,
)
from cortexa.modules.novel_adapter import NovelAdapter
from cortexa.schema import get_schema
from cortexa.storage import SQLiteMemoryStore


def main() -> int:
    ap = argparse.ArgumentParser(description="存量生命周期回填（register + track）")
    ap.add_argument("root", type=Path, help="小说项目根目录（含 .cortexa/cortexa.db）")
    ap.add_argument("--force", action="store_true", help="先清空表全量重建")
    ap.add_argument("--report", action="store_true", help="回填后输出生命周期健康度快照")
    args = ap.parse_args()

    db_path = args.root / ".cortexa" / "cortexa.db"
    if not db_path.exists():
        print(f"未找到记忆库: {db_path}", file=sys.stderr)
        return 1

    scope = args.root.name
    settings = Settings(embedding_provider="hash", _env_file=None)
    store = SQLiteMemoryStore(str(db_path), embedding_dim=settings.embedding_dim)
    adapter = NovelAdapter(store, LLMClient(settings), settings)
    # Cast 白名单：注册只认 schema 已知角色/别名（不发明实体——防 2026-09-04
    # 实锤的噪声泛滥：无白名单时"外门弟子/那双眼睛/不属于你"等话语片段全被注册）
    _schema = get_schema("novel")
    _cast = CastRegistry(_schema)
    _known_names = set(_cast.known_names())

    if args.force:
        with store.lock:
            store.connection.execute("DELETE FROM entity_states WHERE scope = ?", (scope,))
            store.connection.execute("DELETE FROM lifecycle_events WHERE scope = ?", (scope,))
            store.connection.commit()
        print("已清空 entity_states/lifecycle_events，全量重建")

    rows = store.connection.execute(
        "SELECT session_id, content, entities_json FROM memories "
        "WHERE scope = ? AND status = ? AND session_id LIKE ? ORDER BY session_id",
        (scope, "ACTIVE", "ch%"),
    ).fetchall()

    per_chapter: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        m = re.match(r"ch(\d+)", r["session_id"] or "", re.IGNORECASE)
        if not m:
            continue
        ch = int(m.group(1))
        per_chapter[ch].append({
            "content": r["content"],
            "entities": json.loads(r["entities_json"]) if r["entities_json"] else [],
        })

    if not per_chapter:
        print(f"scope 内无章节记忆: {scope}", file=sys.stderr)
        store.close()
        return 1

    max_done = 0
    if not args.force:
        row = store.connection.execute(
            "SELECT MAX(last_chapter) as mx FROM entity_states WHERE scope = ?", (scope,)
        ).fetchone()
        max_done = row["mx"] or 0

    total_registered = 0
    chapters_done = 0
    # 2026-09-04 重构：角色在场检测不再依赖 memories.entities_json（英文抽取器对
    # 中文产出引号碎片/标点，实锤不可用），改为在每章记忆文本中匹配 Cast 名单
    # 主名与别名——与 settle 工具同源（文本在场驱动），DB 记忆即章节句子的载体。
    canon_pool = sorted(_cast.canonical_names(), key=len, reverse=True)
    alias_map = dict(_schema.aliases)

    for ch in sorted(per_chapter):
        if ch <= max_done:
            continue
        items = per_chapter[ch]
        chapter_text = "\n".join(it["content"] for it in items)

        # 文本在场检测：主名或别名出现即认为该角色本章在场（归一到主名）
        canonical_unique: list[str] = []
        for c in canon_pool:
            if c in chapter_text and c not in canonical_unique:
                canonical_unique.append(c)
        for alias, canon in alias_map.items():
            if alias in chapter_text and canon not in canonical_unique:
                canonical_unique.append(canon)

        if canonical_unique:
            resolved = [
                ResolvedEntity(name=e, entity_type=EntityType.CHARACTER, confidence=0.6)
                for e in canonical_unique[:40]
            ]
            adapter.register_new_entities(resolved, ch, scope)
            total_registered += len(resolved)

        # 生命周期状态转换检测：entities=本章在场角色（track 的已知角色匹配输入）
        mems = [
            MemoryInstance(
                scope=scope, content=it["content"],
                entities=list(canonical_unique),
                mem_type=MemoryType.FACT, content_hash="",
            )
            for it in items
        ]
        adapter.track_characters(mems, ch, scope)
        chapters_done += 1

    stats = store.connection.execute(
        "SELECT status, COUNT(*) as n FROM entity_states WHERE scope = ? GROUP BY status",
        (scope,),
    ).fetchall()
    ev_total = store.connection.execute(
        "SELECT COUNT(*) FROM lifecycle_events WHERE scope = ?", (scope,)
    ).fetchone()[0]
    state_total = sum(r["n"] for r in stats) if stats else 0

    print()
    print("=== 回填完成 ===")
    print(f"章节处理: {chapters_done}（跳过已完成 {max(0, max_done)} 章）")
    print(f"注册实体: {total_registered}（entity_states 共 {state_total} 条）")
    print(f"实体状态分布: { {r[0]: r[1] for r in stats} }")
    print(f"生命周期事件: {ev_total} 条")

    if args.report:
        report(store, scope)

    store.close()
    return 0


def report(store, scope: str) -> None:
    """回填后的生命周期健康度快照（验证视角）。"""
    rows = store.connection.execute(
        "SELECT status, COUNT(*) as n FROM entity_states WHERE scope = ? GROUP BY status",
        (scope,),
    ).fetchall()
    print("\n=== 生命周期健康度快照 ===")
    print(f"实体状态分布: { {r[0]: r[1] for r in rows} }")

    danger = store.connection.execute(
        "SELECT entity_name, status, last_chapter FROM entity_states "
        "WHERE scope = ? AND status IN (?,?)",
        (scope, "EXITED", "DORMANT"),
    ).fetchall()
    if danger:
        print("需关注实体 (EXITED/DORMANT):")
        for r in danger:
            print(f"  {r[0]} [{r[1]}] 最后出现 Ch{r[2]}")
    else:
        print("需关注实体 (EXITED/DORMANT): 无")

    hot = store.connection.execute(
        "SELECT entity_name, appearance_count, last_chapter FROM entity_states "
        "WHERE scope = ? ORDER BY appearance_count DESC LIMIT 8",
        (scope,),
    ).fetchall()
    if hot:
        print("高频实体 (主角候选，按出场数):")
        for r in hot:
            print(f"  {r[0]} (n={r[1]}, 最后 Ch{r[2]})")

    ev = store.connection.execute(
        "SELECT COUNT(*) FROM lifecycle_events WHERE scope = ?", (scope,),
    ).fetchone()[0]
    exited = store.connection.execute(
        "SELECT COUNT(*) FROM entity_states WHERE scope = ? AND status = ?",
        (scope, "EXITED"),
    ).fetchone()[0]
    print(f"生命周期事件: {ev} 条 | EXITED 实体: {exited} 个")
    print("（若 EXITED 实体出现在后续检索结果，LifecycleGuard 会自动附加⚠️预警）")


if __name__ == "__main__":
    sys.exit(main())
