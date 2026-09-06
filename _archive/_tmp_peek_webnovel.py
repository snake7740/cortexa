# -*- coding: utf-8 -*-
"""只读探查 webnovel-writer 的 index.db：表结构 + 每表样例行（解耦契约：不写不改）。

用法：CORTEXA_BOOK_ROOT=C:\path\to\novel python _tmp_peek_webnovel.py
"""
import os
import sqlite3

_book = os.environ.get("CORTEXA_BOOK_ROOT", r"C:\Users\junzh\新建文件夹\废灵根修仙-重构版")
DB = f"file:{os.path.join(_book, '.webnovel', 'index.db')}?mode=ro"
con = sqlite3.connect(DB, uri=True)
cur = con.cursor()
tables = [r[0] for r in cur.execute(
    "select name from sqlite_master where type='table' order by name")]
for t in tables:
    print(f"===== 表 {t} =====")
    ddl = cur.execute(
        "select sql from sqlite_master where type='table' and name=?", (t,)).fetchone()[0]
    print(ddl)
    n = cur.execute(f"select count(*) from [{t}]").fetchone()[0]
    print(f"-- 行数: {n}")
    for row in cur.execute(f"select * from [{t}] limit 2"):
        s = str(row)
        print("-- 样例:", s[:400])
    print()
con.close()
