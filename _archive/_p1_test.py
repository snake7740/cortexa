import sys, tempfile
sys.path.insert(0, "C:/Cortexa/cortexa-mvp")
from pathlib import Path
from cortexa.config import Settings
from cortexa.storage import SQLiteMemoryStore
from cortexa.models import MemoryInstance, MemoryType, content_hash

tmp = Path(tempfile.mkdtemp())
(tmp/".cortexa").mkdir()
s = Settings(embedding_provider="hash", _env_file=None)
store = SQLiteMemoryStore(str(tmp/".cortexa"/"cortexa.db"), embedding_dim=8)

def add(ch, content, entities):
    store.add(MemoryInstance(scope=tmp.name, session_id=f"ch{ch}", content=content,
               entities=entities, mem_type=MemoryType.FACT,
               content_hash=content_hash(tmp.name, content)), embedding=None)
add(1, "叶长生走进青云宗。", ["叶长生","青云宗"])
add(2, "叶长生突破炼气三层。", ["叶长生"])
add(3, "苏晚晴离开队伍独自北上。", ["苏晚晴"])
store.close()

# 回填
import sys as _s
sys.path.insert(0, "C:/Cortexa/cortexa-mvp/examples")
import novel_backfill as nb
_s.argv = ["novel_backfill.py", str(tmp)]
nb.main()

# P1: 生命周期健康度快照（回填后能查什么）
store2 = SQLiteMemoryStore(str(tmp/".cortexa"/"cortexa.db"), embedding_dim=8)
print("\n=== 回填后可验证的视角 ===")

# 1. 全量状态分布
rows = store2.connection.execute("SELECT status, COUNT(*) n FROM entity_states GROUP BY status").fetchall()
print("1. 实体状态分布:", {r[0]: r[1] for r in rows})

# 2. 是否有 EXITED/DORMANT 需要关注
danger = store2.connection.execute("SELECT entity_name, status, last_chapter FROM entity_states WHERE status IN (?,?)", ("EXITED","DORMANT")).fetchall()
print("2. 需关注实体(EXITED/DORMANT):", [dict(r) for r in danger] if danger else "无")

# 3. 单实体完整时间线（叶长生）
tl = store2.connection.execute("SELECT entity_name, old_status, new_status, chapter, evidence FROM lifecycle_events WHERE entity_name=? ORDER BY chapter", ("叶长生",)).fetchall()
print("3. 叶长生时间线:")
for r in tl:
    print(f"   ch{r[3]}: {r[1]}->{r[2]} | {r[4][:20]}")

# 4. 出场最密集实体（可能的主角候选）
hot = store2.connection.execute("SELECT entity_name, appearance_count, last_chapter FROM entity_states ORDER BY appearance_count DESC LIMIT 3").fetchall()
print("4. 高频实体:", [dict(r) for r in hot])
store2.close()
print("\n=== P1 验证完成 ===")