"""GovernanceEngine（L2 领域模型）：写入期治理检测（AML 维度 D）。

设计文档：docs/design/scene-aware-architecture.md（v2.1 S3 写入期治理）

场景：评测数据连续 Add 同一用户的信息——用户偏好/状态会变更（"喜欢 X"→"改喜欢 Y"）。
治理 = 新陈述到达时，识别它取代的旧记忆并 supersede（旧标 SUPERSEDED 保留可追溯）。

方法与防幻觉：
- 同 subject + 同"属性槽"（topic/实体）的新旧记忆视为候选冲突；
- 用 schema.event_predicates 的 supersede/update 词判断新陈述是否"否定/取代"旧陈述；
- 只有明确否定/更新的新陈述才触发 supersede（不猜测、不静默改状态）；
- 治理结果写 lifecycle_events（audit 链），旧记忆 content 永不丢（维度 A 红线）。
"""

from __future__ import annotations

import re

from ..models import MemoryInstance, MemoryStatus
from ..schema import SceneSchema, get_schema
from ..storage import MemoryStore


class GovernanceEngine:
    """同一 subject 的写入期治理：新旧冲突检测 + supersede 触发。"""

    def __init__(self, store: MemoryStore, schema: SceneSchema | None = None) -> None:
        self._store = store
        self._schema = schema or get_schema("aml")

    # 否定/取代词（新陈述含这些 → 倾向取代旧陈述）
    _NEGATE = re.compile(r"(?:不|不再|no longer|don't|doesn't|不是|改为|换成|现在是|actually)")
    # 同属性槽的相似度启发（MVP：topic 相同 或 共享实体）

    def _subject_key(self, mem: MemoryInstance) -> str:
        """提取 subject（user/实体）：entities[0] 或 topic，缺省 content 前 6 字。"""
        if mem.entities:
            return mem.entities[0]
        if mem.topic:
            return mem.topic
        return mem.content[:6]

    def _slot_key(self, mem: MemoryInstance) -> str:
        """属性槽：topic 或 entities[1] 或 content 前 10 字（同槽新旧才可比）。"""
        if mem.topic:
            return mem.topic
        if len(mem.entities) > 1:
            return mem.entities[1]
        return mem.content[:10]

    def find_conflicts(self, new_mem: MemoryInstance, scope: str) -> list[MemoryInstance]:
        """找同 subject 同 slot 的旧活跃记忆（候选冲突对象）。"""
        subject = self._subject_key(new_mem)
        slot = self._slot_key(new_mem)
        if not subject:
            return []
        rows = self._store.connection.execute(
            "SELECT id FROM memories WHERE scope = ? AND status = ? "
            "AND id != ?",
            (scope, MemoryStatus.ACTIVE.value, new_mem.id),
        ).fetchall()
        conflicts = []
        for r in rows:
            old = self._store.get(r["id"], scope)
            if old is None:
                continue
            if self._subject_key(old) != subject:
                continue
            # 同槽 或 共享实体 或 语义高度重叠
            if self._slot_key(old) == slot or self._overlap(old, new_mem):
                conflicts.append(old)
        return conflicts

    @staticmethod
    def _overlap(a: MemoryInstance, b: MemoryInstance) -> bool:
        """共享实体数 >= 1 且内容长度接近（同一事实的新旧表述）。"""
        ea, eb = set(a.entities or []), set(b.entities or [])
        shared = len(ea & eb)
        if shared == 0:
            return False
        # 长度差 < 60%（同一槽位内容的合理变异范围）
        la, lb = len(a.content), len(b.content)
        return abs(la - lb) / max(la, lb, 1) < 0.6

    # 内容词停用（主题重叠判定用；与 encoding._ENTITY_STOP 同思路的子集）
    _TOPIC_STOP = frozenset({
        "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at",
        "for", "with", "by", "is", "are", "was", "were", "be", "been", "am",
        "do", "does", "did", "have", "has", "had", "it", "its", "as", "this",
        "that", "not", "no", "now", "currently", "actually", "prefers", "prefer",
    })

    @staticmethod
    def _content_terms(text: str, skip: frozenset[str]) -> set[str]:
        """英文内容词集合（小写、去停用）；中文按整段处理（无分词，保守）。"""
        terms = {w for w in re.findall(r"[A-Za-z][A-Za-z']+", text.lower())
                 if w not in skip}
        return terms

    def _topic_overlap(self, new_text: str, old_text: str, new_mem, old) -> bool:
        """新旧陈述是否谈同一属性（防治理过度：同 subject 不同属性不杀）。

        三重证据任一成立即视为同主题：
        1) 文本包含关系（子串级，中文也适用）；
        2) 共享内容词（英文）；
        3) 共享非主体实体。
        """
        if len(old_text) > 0 and len(new_text) > 0:
            if old_text in new_text or new_text in old_text:
                return True
        # 主体本身不算主题证据（防同 subject 不同属性误杀）
        subject = self._subject_key(new_mem).lower()
        subject_terms = {e.lower() for e in (new_mem.entities or old.entities or [])}
        skip = self._TOPIC_STOP | subject_terms
        shared = self._content_terms(new_text, skip) & self._content_terms(old_text, skip)
        if shared:
            return True
        new_set = {e.lower() for e in (new_mem.entities or []) if e.lower() != subject}
        old_set = {e.lower() for e in (old.entities or []) if e.lower() != subject}
        return bool(new_set & old_set)

    def should_supersede(self, new_mem: MemoryInstance, old: MemoryInstance) -> bool:
        """新陈述是否明确取代旧陈述（含否定/更新词）。不猜——无信号不取代。

        防过度（2026-09-04 实测修复）：词信号命中只是"候选"——同一 subject 可能
        有多条不同属性的陈述（如 Alice 的饮料偏好 vs Alice 的宠物），治理词必须
        同时命中"新旧谈同一属性"才 supersede，避免误杀同主体无关记忆。
        """
        new_text = new_mem.content
        old_text = old.content
        # 信号 1/2：否定词 或 schema 治理词（supersede/update/withdraw 类）
        signaled = False
        if self._NEGATE.search(new_text):
            signaled = True
        else:
            for w in (self._schema.event_predicates.get("supersede", ())
                      + self._schema.event_predicates.get("update", ())
                      + self._schema.event_predicates.get("withdraw", ())):
                if w.lower() in new_text.lower():
                    signaled = True
                    break
        if not signaled:
            return False
        # 信号 3：内容同主题（防治理过度——同 subject 不同属性不得互相取代）
        return self._topic_overlap(new_text, old_text, new_mem, old)

    def govern(self, new_mem: MemoryInstance, scope: str) -> dict:
        """对新记忆执行治理：找冲突旧记忆，若新陈述取代则 supersede 旧记忆。"""
        result = {"subject": self._subject_key(new_mem), "superseded": 0, "kept": 0}
        for old in self.find_conflicts(new_mem, scope):
            if self.should_supersede(new_mem, old):
                try:
                    self._store.supersede(old.id, new_mem.id, scope)
                    result["superseded"] += 1
                except Exception:
                    result["kept"] += 1
            else:
                result["kept"] += 1
        return result
