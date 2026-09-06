"""R3：内容驱动 forget（2026-09-06）。
AML D 治理含"用户要求删除后是否真不再用"——Add 消息中检测显式删除/遗忘指令并
FORGOTTEN 目标记忆。规则从严：please forget/remove/delete/erase + (my/this/…)
或中文 忘掉/删除/别再记；目标=指令后内容词(>=4 且非停用)；与 ACTIVE 记忆内容词
交集>=1 即遗忘；闲聊 forget it（目标空/仅停用词）不动作；RULE/no_decay 由
store.forget 红线拒绝。
"""
from __future__ import annotations

import re

_FORGET = re.compile(
    r"(?:please\s+)?(?:forget|remove|delete|erase)\s+(?:my|this|that|the)?\s*(.*?)$"
    r"|(?:忘掉|删除|别再记|别再提|不要记)(?:关于)?(.*)$",
    re.IGNORECASE,
)
_STOP = frozenset({
    "the", "this", "that", "these", "those", "it", "its", "them", "they",
    "from", "about", "with", "and", "for", "my", "our", "your", "his", "her",
    "please", "also", "just", "really", "one", "two", "thing", "stuff", "now",
})


def parse_forget(content: str) -> str | None:
    """返回待遗忘目标词串；非指令或目标无效返回 None。"""
    text = content or ""
    m = _FORGET.search(text)
    if not m:
        return None
    target = (m.group(1) or m.group(2) or "").strip()
    if not target:
        return None
    # 英文目标词>=5 字母且非停用（>=4 会把 "move" 之类闲聊词放进来）；
    # 无英文实词时退而接受中文连续片段（>=2 字）作目标。
    words = [w for w in re.findall(r"[a-z0-9']+", target.lower())
             if len(w) >= 5 and w not in _STOP]
    if words:
        return " ".join(words)
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", target)
    return cjk[0] if cjk else None


def forget_matching(store, scope: str, target: str) -> int:
    """遗忘 scope 内内容含目标(词交集或中文子串)的 ACTIVE 记忆；返回条数。"""
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", target))
    words = [] if has_cjk else [w for w in target.split() if len(w) >= 4]
    if not has_cjk and not words:
        return 0
    rows = store.connection.execute(
        "SELECT id, content FROM memories WHERE scope=? AND status='ACTIVE'",
        (scope,),
    ).fetchall()
    n = 0
    for r in rows:
        low = (r["content"] or "").lower()
        hit = (target in low) if has_cjk else any(w in low for w in words)
        if hit:
            try:
                store.forget(r["id"], scope)
                n += 1
            except Exception:
                pass  # RULE/no_decay 红线拒触：跳过
    return n
