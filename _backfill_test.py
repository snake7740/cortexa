import sys, tempfile, os
sys.path.insert(0, "C:/Cortexa/cortexa-mvp")
sys.path.insert(0, "C:/Cortexa/cortexa-mvp/examples")
from pathlib import Path
from cortexa.config import Settings
from cortexa.storage import SQLiteMemoryStore
from cortexa.models import MemoryInstance, MemoryType, content_hash
import novel_backfill as nb

tmp = Path(tempfile.mkdtemp())
db_dir = tmp / ".cortexa"; db_dir.mkdir()
db_path = db_dir / "cortexa.db"
s = Settings(embedding_provider="hash", _env_file=None)
store = SQLiteMemoryStore(str(db_path), embedding_dim=8)

def add(ch, content, entities):
    store.add(MemoryInstance(scope=tmp.name, session_id=f"ch{ch}", content=content,
               entities=entities, mem_type=MemoryType.FACT,
               content_hash=content_hash(tmp.name, content)), embedding=None)

add(1, "叶长生走进青云宗大门。", ["叶长生", "青云宗"])
add(1, "对。", ["对。"])
add(1, "我不知道。", ["我不知道。"])
add(2, "叶长生突破到炼气三层。", ["叶长生"])
add(2, "苏晚晴跟了上来。", ["苏晚晴"])
add(3, "老乞丐死了，叶长生埋葬了他。", ["老乞丐", "叶长生"])
store.close()

import sys as _s
_s.argv = ["novel_backfill.py", str(tmp)]
rc = nb.main()
print("backfill rc:", rc)

store2 = SQLiteMemoryStore(str(db_path), embedding_dim=8)
rows = store2.connection.execute("SELECT entity_name, status, first_chapter, last_chapter, appearance_count FROM entity_states ORDER BY first_chapter").fetchall()
print("=== entity_states ===")
for r in rows:
    print(f"  {r[0]}: {r[1]} ch{r[2]}-{r[3]} n={r[4]}")
events = store2.connection.execute("SELECT entity_name, old_status, new_status, chapter FROM lifecycle_events ORDER BY chapter").fetchall()
print("=== lifecycle_events ===")
for r in events:
    print(f"  ch{r[3]}: {r[0]} {r[1]} -> {r[2]}")
frag_names = [r[0] for r in rows if r[0] in ("对", "我不知道")]
print("碎片过滤:", "OK" if not frag_names else f"FAIL({frag_names})")
names = [r[0] for r in rows]
print("实体:", sorted(names))
store2.close()