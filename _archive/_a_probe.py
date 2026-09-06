# -*- coding: utf-8 -*-
"""A 基线题的相关对余弦：若全部 >0.3，设 0.3 无伤。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cortexa.config import Settings
from cortexa.llm.client import HashEmbedder
import local_eval  # noqa
sys.path.insert(0, str(Path(__file__).resolve().parent))

s = Settings(embedding_provider="hash", _env_file=None)
emb = HashEmbedder(s.embedding_dim)
def cos(a, b): return sum(x*y for x, y in zip(a, b))

# ANSWERABLE 的 (query, gold) 余弦——gold 在库里的记忆，query 检索它
print("A 基线 11 题 query->gold 余弦:")
below = []
for q, g in local_eval.ANSWERABLE:
    c = cos(emb.embed(q), emb.embed(g))
    mark = " <0.3!" if c < 0.3 else ""
    if c < 0.3: below.append((c, q, g))
    print(f"  {c:.3f}{mark}  Q={q[:32]!r}")
print(f"\n低于 0.3 的: {len(below)}/{len(local_eval.ANSWERABLE)}")
for c, q, g in below:
    print(f"  {c:.3f} {q[:30]!r} vs {g[:30]!r}")
