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
from ..storage import SUBJECT_SQL_EXPR, MemoryStore


class GovernanceEngine:
    """同一 subject 的写入期治理：新旧冲突检测 + supersede 触发。"""

    def __init__(self, store: MemoryStore, schema: SceneSchema | None = None) -> None:
        self._store = store
        self._schema = schema or get_schema("aml")
        # 主体谓词回退次数（0 = 全程走索引谓词；非 0 说明环境不支持 JSON1）
        self.subject_predicate_fallbacks = 0

    # 否定/取代词（新陈述含这些 → 倾向取代旧陈述）。
    # v2（2026-09-06 治理精度审计）：EN 一律整词/整短语匹配（词边界）——
    # 子串误命中案例：schema 'now' 命中 "I already k**now** he loves..."。
    _NEGATE_TOKENS = (
        "no longer", "don't", "doesn't", "isn't", "can't", "cannot",
        "not allowed", "actually", "不", "不再", "不是", "改为", "换成", "现在是",
    )
    # 假设/问句防护：这些形态不是"当前状态已更新"的断言，不得触发取代
    # （案例：'do you think it's possible to also set a time limit...' 是对未来的
    #  打算/询问，不是"日均屏幕时长已被更新"）。
    _HYPOTHETICAL = (
        "do you think", "what do you think", "is it possible", "should i",
        "could we", "maybe i", "do you have any", "any recommendations",
        "would you recommend", "does that sound", "would that be", "i wonder",
    )
    # 跨属性误杀防护用通用词（属性论元过滤器：'time/social/limit' 之类在闲聊
    # 与事实陈述里都会出现，不能当同一属性的证据）。
    _GENERIC_ATTR = frozenset({
        "time", "times", "screen", "app", "apps", "social", "media", "day",
        "days", "hour", "hours", "week", "weeks", "month", "months", "thing",
        "things", "stuff", "bit", "lot", "really", "much", "more", "new",
        "know", "think", "would", "great", "special", "limit", "limits",
        "idea", "ideas", "possible", "spending", "way", "setting", "set",
        "like", "love", "get", "got", "getting", "recommendation",
        "recommendations", "starting", "point", "past", "two", "one",
    })

    def _subject_key(self, mem: MemoryInstance) -> str:
        """提取 subject（user/实体）：entities[0] 或 topic，缺省 content 前 6 字。"""
        if mem.entities:
            return mem.entities[0]
        if mem.topic:
            return mem.topic
        return mem.content[:6]

    def _slot_key(self, mem: MemoryInstance) -> str:
        """属性槽 v2：优先属性实体 entities[1]；否则 topic（若不同于主语）；
        否则内容前缀。修复 v1 缺陷：topic=主语首实体使同主体所有陈述同槽
        （案例：Max 的品种 vs Max 的玩具被当成同一槽）。"""
        if len(mem.entities) >= 2:
            return mem.entities[1].lower()
        subject = (self._subject_key(mem) or "").lower()
        if mem.topic and mem.topic.lower() != subject:
            return mem.topic.lower()
        return (mem.content or "")[:10].lower()

    def find_conflicts(self, new_mem: MemoryInstance, scope: str) -> list[MemoryInstance]:
        """找同 subject 同 slot 的旧活跃记忆（候选冲突对象）。

        P1-2（2026-09-06）：原实现对每候选一次 store.get（N 次独立查询 + 解码）；
        改为单次 SELECT * 全量取回后统一用 _row_to_mem 解码（与 store.get 同一构建器，
        语义 1:1）——逐 chunk Add 的治理开销从 O(N) 次查询降到 1 次查询 + O(N) 解码
        （解码仅活跃集一次，缓存友好；万条级实测见 tests/test_hotpath_complexity.py）。

        2026-10-31 修复：上面那次优化仍要拉回 scope 内全部 ACTIVE 行并逐行构造
        Pydantic，且全程在 store 的全局 RLock 内。该项成本随**单个 scope** 的行数涨，
        不随全库行数涨（R5 实测：最大 scope 2216 行 = 25.1 ms 查询 + 59 ms 解码；
        此前按 132,559 行全库外推的“2.93 s/条”高估 30—50 倍，已作废）。现把主体
        判定下推为 SQL 谓词 (SUBJECT_SQL_EXPR) = ?，只把同主体的少数行取回；不依赖
        表达式索引（走 scope 索引 + 行过滤，结果集逐行相同）。
        Python 侧 _subject_key 复核与 slot/overlap 判定循环逐字保留——SQL 谓词只起
        "少取行"的过滤作用，结果集只能与原口径相同；谓词不可用时回退原全量查询。
        """
        subject = self._subject_key(new_mem)
        slot = self._slot_key(new_mem)
        if not subject:
            return []
        base = (
            "SELECT * FROM memories WHERE scope = ? AND status = ? "
            "AND id != ?"
        )
        params: list = [scope, MemoryStatus.ACTIVE.value, new_mem.id]
        rows = None
        if getattr(self._store, "subject_lookup_available", False):
            try:
                rows = self._store.connection.execute(
                    f"{base} AND ({SUBJECT_SQL_EXPR}) = ?", [*params, subject]
                ).fetchall()
            except Exception:  # noqa: BLE001 - 回退口径，但必须留下计数
                self.subject_predicate_fallbacks += 1
                rows = None
        if rows is None:
            rows = self._store.connection.execute(base, params).fetchall()
        conflicts = []
        for r in rows:
            old = self._store._row_to_mem(r)
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

    @staticmethod
    def _word_tokens(text: str) -> set[str]:
        """英文/数字/撇号词元集合（词边界信号用，避免 now 命中 know）。"""
        return set(re.findall(r"[a-z0-9']+", (text or "").lower()))

    @classmethod
    def _word_hit(cls, word: str, toks: set[str], joined: str, raw_low: str) -> bool:
        """整词/整短语匹配；无 ascii 的词（中文）保留子串语义（v1 行为）。"""
        if any(ch.isascii() and ch.isalpha() for ch in word):
            if " " in word:
                return word in joined
            return word in toks
        return word in raw_low

    def _signal(self, text: str) -> tuple[str, str] | None:
        """治理信号（v2，词边界）：negate 词优先，再 schema 治理词。"""
        raw = text or ""
        low = raw.lower()
        toks = self._word_tokens(low)
        joined = " ".join(re.findall(r"[a-z0-9']+", low))
        for w in self._NEGATE_TOKENS:
            if self._word_hit(w, toks, joined, low):
                return ("negate", w)
        for w in (self._schema.event_predicates.get("supersede", ())
                  + self._schema.event_predicates.get("update", ())
                  + self._schema.event_predicates.get("withdraw", ())):
            if self._word_hit(w, toks, joined, low):
                return ("schema", w)
        return None

    @staticmethod
    def _hypothetical(text: str) -> bool:
        """新陈述是问句/对未来打算/征询意见 → 不是当前状态更新，拒绝取代。

        实测案例 545bd2b5：'do you think it's possible to also set a time
        limit...' 含 don't（词边界信号命中）但语义是询问建议，不是『日均
        2 小时已被更新』——不取代旧陈述。
        """
        low = (text or "").lower()
        if "?" in text or "？" in text:
            return True
        return any(p in low for p in GovernanceEngine._HYPOTHETICAL)

    def _attr_terms(self, mem: MemoryInstance) -> set[str]:
        """属性论元词（同属性判据）：非停用/非主语实体/非通用词的内容词。"""
        subject_terms = {e.lower() for e in (mem.entities or [])}
        skip = self._TOPIC_STOP | subject_terms | self._GENERIC_ATTR
        return {
            w for w in re.findall(r"[a-z][a-z']{2,}", (mem.content or "").lower())
            if w not in skip
        }

    def should_supersede(self, new_mem: MemoryInstance, old: MemoryInstance) -> bool:
        """新陈述是否明确取代旧陈述（v2，2026-09-06 治理精度审计修复）。

        三层防过度（对应真实误杀案例）：
        1) 信号=整词/整短语匹配——'now' 不再命中 'k**now**'（75499fd8 误杀根因）；
        2) 问句/未来打算/征询不取代——'don't' + 'do you think...' 不更新
           『日均 2 小时』事实（545bd2b5 误杀根因）；
        3) 同属性才取代：同槽（属性实体）或共享属性论元词或共享非主语实体；
           纯子串包含恒取代（同句改写）。不猜——以上任一不满足即 kept。
        """
        new_text = new_mem.content
        old_text = old.content
        sig = self._signal(new_text)
        if sig is None:
            return False
        if self._hypothetical(new_text):
            return False
        if old_text in new_text or new_text in old_text:
            return True
        if self._slot_key(new_mem) == self._slot_key(old):
            return True
        if self._attr_terms(new_mem) & self._attr_terms(old):
            return True
        subject = self._subject_key(new_mem).lower()
        new_set = {e.lower() for e in (new_mem.entities or []) if e.lower() != subject}
        old_set = {e.lower() for e in (old.entities or []) if e.lower() != subject}
        return bool(new_set & old_set)

    def _signal_desc(self, text: str) -> str:
        """治理命中的信号描述（审计 reason 用，journal 'govern' 事件）。"""
        sig = self._signal(text or "")
        return f"{sig[0]}:{sig[1]}" if sig else "supersede-signal"

    def govern(self, new_mem: MemoryInstance, scope: str, *,
              soft: bool = False) -> dict:
        """对新记忆执行治理：找冲突旧记忆，若新陈述取代则 supersede 旧记忆。

        soft=True（B2 实验，2026-09-06）：不硬隐藏——旧值 retrieval_weight 降为
        30% 仍 ACTIVE 可返（对照 AMI『治理价值=改返回集』与 FlowGrid 降权不删）；
        默认 False=硬隐藏(SUPERSEDED 不进 Search，现行语义)。两种都落 journal 审计链。
        """
        result = {"subject": self._subject_key(new_mem), "superseded": 0, "kept": 0,
                  "events": []}
        for old in self.find_conflicts(new_mem, scope):
            if self.should_supersede(new_mem, old):
                try:
                    reason = f"{self._signal_desc(new_mem.content)}|same-attr"
                    if soft:
                        with self._store.lock:
                            self._store.connection.execute(
                                "UPDATE memories SET retrieval_weight = ? "
                                "WHERE id = ? AND scope = ?",
                                (max(0.1, (old.retrieval_weight or 1.0) * 0.3),
                                 old.id, scope),
                            )
                            self._store.connection.commit()
                        result["events"].append({
                            "old_id": old.id, "new_id": new_mem.id,
                            "reason": reason + "|soft",
                        })
                        result["kept"] += 1  # soft=不隐藏：统计口径置 kept(仍在库可返)
                    else:
                        self._store.supersede(old.id, new_mem.id, scope)
                        result["superseded"] += 1
                        result["events"].append({
                            "old_id": old.id, "new_id": new_mem.id,
                            "reason": reason,
                        })
                except Exception as _exc:  # noqa: BLE001 —— 失败转 kept 的分支，进观察面
                    from ..enhance_stats import stats
                    stats.inc("governance_skip", err=_exc)
                    result["kept"] += 1
            else:
                result["kept"] += 1
        return result