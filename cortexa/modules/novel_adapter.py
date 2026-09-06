"""网文领域适配器（NovelAdapter）。

设计文档：docs/design/novel-adapter-architecture.md

架构原则：
- 独立模块，不修改现有管线核心逻辑
- 通过钩子(hook)集成：pipeline.run_add 末尾调 post_ingest，search_memory 返回前调 pre_return
- 所有方法纯函数式，构造时注入依赖（store/llm/settings），不持有状态
- 每个子模块可独立启用/禁用（Settings.adapter_*_enabled）
- 存储扩展向后兼容：新表与旧表同库同锁

防幻觉红线：
- 实体抽取只从原文提取，不发明
- 生命周期转换必须有 evidence + chapter
- EXITED 角色检索时附带警告，但不删除结果
- 文笔特征只做统计，不做主观评价
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..config import Settings
from ..llm import LLMClient
from ..schema import SceneSchema, get_schema
from ..models import (
    EntityType,
    EntitySnapshot,
    LifecycleEvent,
    LifecycleStatus,
    MemoryInstance,
    ResolvedEntity,
    SearchResult,
    StyleProfile,
)
from ..storage import MemoryStore

# ---------------------------------------------------------------------------
# 章节号提取（与 search.py extract_chapter_nos 同源逻辑）
# ---------------------------------------------------------------------------

_CH_FROM_SESSION = re.compile(r"ch(\d+)", re.IGNORECASE)


def _extract_chapter(session_id: str | None) -> int | None:
    """从 session_id 提取章节号。返回 None 表示非章节会话。"""
    if not session_id:
        return None
    m = _CH_FROM_SESSION.search(session_id)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# NovelAdapter 统一入口
# ---------------------------------------------------------------------------

class NovelAdapter:
    """网文领域适配器。构造时注入依赖，不创建自己的连接。

    与现有管线共享同一 store.lock，保证事务一致性。
    """

    def __init__(
        self,
        store: MemoryStore,
        llm: LLMClient,
        settings: Settings,
        schema: SceneSchema | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._settings = settings
        # L3 场景语义注入：模式/词表从 schema 读，不硬编码（设计 v2 §4）
        self._schema = schema or get_schema("novel")
        # 预编译关系句式（schema 数据 -> 可执行正则）
        self._rel_regex = [
            (re.compile(p), t) for p, t in self._schema.relation_patterns
        ]

    # =====================================================================
    # P0: EntityResolver — LLM实体抽取+消歧
    # =====================================================================

    def resolve_entities(
        self, text: str, chapter: int | None, scope: str
    ) -> list[ResolvedEntity]:
        """LLM实体抽取+消歧。替代 encoding.extract_entities 的启发式结果。

        失败时降级返回空列表（不阻断管线）。
        """
        if not self._llm.available():
            return []

        system = (
            "Extract named entities from this Chinese novel chapter. "
            "Return ONLY a JSON array of objects with fields: "
            "name (string), type (CHARACTER|ITEM|FACTION|LOCATION|STORY_THREAD), "
            "confidence (0.0-1.0). "
            "Focus on proper nouns: character names, place names, item names, faction names. "
            "Do NOT include dialogue fragments, common words, or pronouns. "
            "Maximum 20 entities. Reply with valid JSON only, no explanation."
        )
        # 截断至4000字避免超token
        truncated = text[:4000]
        reply = self._llm.chat(system, truncated, max_tokens=512, temperature=0)
        if not reply:
            return []

        try:
            # 尝试提取JSON数组
            json_match = re.search(r"\[.*]", reply, re.DOTALL)
            if not json_match:
                return []
            items = json.loads(json_match.group())
            results: list[ResolvedEntity] = []
            for item in items:
                if not isinstance(item, dict) or "name" not in item:
                    continue
                name = str(item["name"]).strip()
                if len(name) < 2 or len(name) > 20:
                    continue
                try:
                    etype = EntityType(str(item.get("type", "CHARACTER")).upper())
                except ValueError:
                    etype = EntityType.CHARACTER
                conf = float(item.get("confidence", 0.8))
                conf = max(0.0, min(1.0, conf))
                results.append(ResolvedEntity(name=name, entity_type=etype, confidence=conf))
            return results[:20]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []

    # =====================================================================
    # P0: CharacterTracker — 角色生命周期追踪
    # =====================================================================

    # 状态转换触发词（确定性匹配，非LLM推断）
    _TRANSFORM_KEYWORDS = re.compile(
        r"突破|晋升|觉醒|蜕变|渡劫|飞升|黑化|入魔|顿悟|解封|封印"
    )
    _EXIT_KEYWORDS = re.compile(
        r"死亡|陨落|身亡|殒命|魂飞魄散|同归于尽|牺牲|战死|自爆|消散"
        r"|永久离开|永远消失|再无音讯"
    )
    _DEPART_KEYWORDS = re.compile(
        r"离开|远去|告别|分别|启程|出发|消失|不见踪影|杳无音信"
    )

    def track_characters(
        self, memories: list[MemoryInstance], chapter: int | None, scope: str
    ) -> list[LifecycleEvent]:
        """从本章记忆中推断角色状态变化。

        只做确定性推断（原文明确写了突破/死亡/离开等关键词）。
        模糊情况不入 lifecycle_events。
        """
        if chapter is None:
            return []

        events: list[LifecycleEvent] = []
        full_text = "\n".join(m.content for m in memories)

        # 收集本章出现的已知角色
        known_chars = self._get_known_characters(scope)
        mentioned_chars: set[str] = set()
        for mem in memories:
            for ent in mem.entities:
                if ent in known_chars:
                    mentioned_chars.add(ent)

        for char_name in mentioned_chars:
            current = self._get_entity_state(scope, char_name)

            # 检查状态转换
            new_status = None
            evidence = ""
            detail: dict[str, Any] = {}

            if self._TRANSFORM_KEYWORDS.search(full_text) and char_name in full_text:
                # 找到包含该角色名+转换词的段落作为证据
                for mem in memories:
                    if char_name in mem.content and self._TRANSFORM_KEYWORDS.search(mem.content):
                        new_status = LifecycleStatus.TRANSFORMED
                        evidence = mem.content[:200]
                        break

            elif self._EXIT_KEYWORDS.search(full_text) and char_name in full_text:
                for mem in memories:
                    if char_name in mem.content and self._EXIT_KEYWORDS.search(mem.content):
                        new_status = LifecycleStatus.EXITED
                        evidence = mem.content[:200]
                        break

            if new_status is None:
                # 无状态转换，更新出场记录
                self._update_appearance(scope, char_name, EntityType.CHARACTER, chapter)
                continue

            # 验证状态转换合法性
            if current and not self._is_valid_transition(current.status, new_status):
                continue

            old_status = current.status if current else None
            event = LifecycleEvent(
                entity_name=char_name,
                entity_type=EntityType.CHARACTER,
                old_status=old_status,
                new_status=new_status,
                chapter=chapter,
                evidence=evidence,
                detail=detail,
            )
            events.append(event)

            # 写入数据库
            self._record_lifecycle_event(scope, event)
            self._update_entity_state(scope, event)

        # 对新出现的角色（不在known_chars中但被LLM识别为CHARACTER的）注册INTRODUCED
        # 这部分由调用方在 resolve_entities 之后传入 resolved entities

        return events

    def register_new_entities(
        self, entities: list[ResolvedEntity], chapter: int | None, scope: str
    ) -> list[LifecycleEvent]:
        """为新发现的实体注册 INTRODUCED/ACQUIRED 事件。"""
        if chapter is None:
            return []

        events: list[LifecycleEvent] = []
        for ent in entities:
            existing = self._get_entity_state(scope, ent.name)
            if existing is not None:
                # 已存在，更新出场
                self._update_appearance(scope, ent.name, ent.entity_type, chapter)
                # DORMANT → ACTIVE
                if existing.status == LifecycleStatus.DORMANT:
                    event = LifecycleEvent(
                        entity_name=ent.name,
                        entity_type=ent.entity_type,
                        old_status=LifecycleStatus.DORMANT,
                        new_status=LifecycleStatus.ACTIVE,
                        chapter=chapter,
                        evidence=f"重新出现在第{chapter}章",
                    )
                    events.append(event)
                    self._record_lifecycle_event(scope, event)
                    self._update_entity_state(scope, event)
                continue

            # 新实体
            initial_status = (
                LifecycleStatus.INTRODUCED
                if ent.entity_type == EntityType.CHARACTER
                else LifecycleStatus.ACQUIRED
                if ent.entity_type == EntityType.ITEM
                else LifecycleStatus.PLANTED
            )
            event = LifecycleEvent(
                entity_name=ent.name,
                entity_type=ent.entity_type,
                old_status=None,
                new_status=initial_status,
                chapter=chapter,
                evidence=f"首次出现在第{chapter}章",
            )
            events.append(event)
            self._record_lifecycle_event(scope, event)
            self._update_entity_state(scope, event)

        return events

    # =====================================================================
    # P1: StyleProfiler — 文笔特征提取（纯本地，零LLM）
    # =====================================================================

    _SENTENCE_SPLIT = re.compile(r"[。！？!?]+")
    _DIALOGUE_LINE = re.compile(r'^[""「『【]')
    _RHETORIC_WORDS = re.compile(
        r"仿佛|宛如|犹如|好似|恰似|如同|一般|般|似的|一样"
        r"|骤然|猛然|赫然|陡然|倏然|蓦地"
        r"|不禁|忍不住|不由自主"
    )

    def profile_style(
        self, text: str, chapter: int, scope: str
    ) -> StyleProfile | None:
        """计算单章文笔统计特征。纯本地计算，零LLM。

        字数<500的章节跳过（统计不可靠）。
        """
        if len(text) < 500:
            return None

        lines = text.splitlines()
        non_empty = [ln for ln in lines if ln.strip()]
        if not non_empty:
            return None

        # 句长
        sentences = [s.strip() for s in self._SENTENCE_SPLIT.split(text) if s.strip()]
        avg_sent_len = sum(len(s) for s in sentences) / max(len(sentences), 1)

        # 对话比
        dialogue_lines = sum(1 for ln in non_empty if self._DIALOGUE_LINE.match(ln.strip()))
        dialogue_ratio = dialogue_lines / max(len(non_empty), 1)

        # 段落均长
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        para_avg = sum(len(p) for p in paragraphs) / max(len(paragraphs), 1)

        # 修辞密度
        rhetoric_count = len(self._RHETORIC_WORDS.findall(text))
        rhetoric_density = rhetoric_count / max(len(text), 1)

        # POV检测（简化版）
        first_person = len(re.findall(r"我(?:们)?(?:想|说|看|觉得|感到|知道)", text))
        pov = "first" if first_person > 5 else "third_limited"

        # 代表性句式（取中等长度的句子）
        mid_sentences = sorted(sentences, key=lambda s: abs(len(s) - avg_sent_len))
        samples = mid_sentences[:5]

        profile = StyleProfile(
            chapter=chapter,
            avg_sentence_len=round(avg_sent_len, 1),
            dialogue_ratio=round(dialogue_ratio, 3),
            paragraph_avg_len=round(para_avg, 1),
            rhetoric_density=round(rhetoric_density, 5),
            pov=pov,
            sample_sentences=samples,
        )

        # 写入数据库
        self._save_style_profile(scope, profile)
        return profile

    # =====================================================================
    # P2: RARE-lite — 检索增强重编码（关联强化写回）
    # =====================================================================

    def rare_strengthen(
        self, result_ids: list[str], scope: str, top_n: int = 5
    ) -> int:
        """检索后关联强化（RARE-lite，确定性零 LLM）。

        文档 RARE 四步（上下文融合/关联强化/反思注解/写回更新）的关联强化子集：
        同一次检索命中的 TopN 记忆两两写入 links（簇间桥梁，维度 B），
        关联证据 = 用户真实查询（防幻觉：不引入原文外事实）。

        幂等：storage.add_links 去重。返回新增关联总数。
        """
        ids = result_ids[:top_n]
        if len(ids) < 2:
            return 0
        total = 0
        for mem_id in ids:
            others = [i for i in ids if i != mem_id]
            total += self._store.add_links(mem_id, scope, others)
        return total

    # =====================================================================
    # P3: RelationInferrer — 显式关系自动建边
    # =====================================================================

    # 关系句式来自 L3 schema（self._rel_regex，构造时预编译）——场景语义上移
    def infer_relations(
        self,
        text: str,
        known_entities: list[str],
        chapter: int | None,
        scope: str,
    ) -> list[dict]:
        """从原文提取显式关系，生成建边请求（graph.add_link 参数）。

        防幻觉约束：
        - 只处理显式关系表述（拜师/结为道侣/父子/仇敌 等），不从同框出现推断；
        - 关系双方必须都在 known_entities（已注册实体）中，否则跳过；
        - 每条携带 evidence（原文片段），供 graph.add_link 留痕。

        Returns: [{src, dst, link_type, evidence, evidence_chapter}, ...]
        """
        if chapter is None or not known_entities:
            return []
        known_set = set(known_entities)
        found: list[dict] = []
        # 按句子处理
        for sent in re.split(r"[。！？!?\n]+", text):
            for pattern, link_type in self._rel_regex:
                m = re.search(pattern, sent)
                if not m:
                    continue
                a = m.group("A").strip() if m.groupdict().get("A") else None
                b = m.group("B").strip() if m.groupdict().get("B") else None
                # 捕获到的 A/B 必须是已知实体（防幻觉：不发明实体关系）
                if not a or not b or a not in known_set or b not in known_set:
                    continue
                if a == b:
                    continue
                found.append({
                    "src": a,
                    "dst": b,
                    "link_type": link_type,
                    "evidence": sent.strip()[:120],
                    "evidence_chapter": chapter,
                })
                break  # 一句只取第一个命中关系
        # 去重（同 src/dst/type）
        uniq: list[dict] = []
        seen_k = set()
        for r in found:
            key = (r["src"], r["dst"], r["link_type"])
            if key not in seen_k:
                seen_k.add(key)
                uniq.append(r)
        return uniq

    # =====================================================================
    # P1: LifecycleGuard — 检索预警
    # =====================================================================

    def guard_lifecycle(
        self, results: list[SearchResult], scope: str
    ) -> list[SearchResult]:
        """检查返回结果中是否涉及 EXITED/DESTROYED 实体。

        如有，在 SearchResult.content 文本末尾追加警告标记。
        不删除结果（precision-first：给写作系统判断权）。
        注意：P2-5 已登记——警告进 content 会污染「返回证据原文」纯度，参赛部署
        adapter 默认关闭故不触发；修复方向是把标记移入独立提示面（待 A/B）。
        """
        # 获取所有 EXITED/DESTROYED 实体
        exited = self._get_exited_entities(scope)
        if not exited:
            return results

        guarded: list[SearchResult] = []
        for r in results:
            warning_parts: list[str] = []
            for name, status, last_ch in exited:
                if name in r.memory:
                    warning_parts.append(
                        f"⚠️ [{name}] 状态={status.value}，最后出现于Ch{last_ch}"
                    )
            if warning_parts:
                warning = "\n" + "\n".join(warning_parts)
                guarded.append(SearchResult(
                    id=r.id,
                    content=r.content + warning,
                    score=r.score,
                    created_at=r.created_at,
                ))
            else:
                guarded.append(r)

        return guarded

    # =====================================================================
    # 内部辅助方法
    # =====================================================================

    def _get_known_characters(self, scope: str) -> set[str]:
        """获取scope内所有已知角色名。"""
        rows = self._store.connection.execute(
            "SELECT entity_name FROM entity_states WHERE scope = ? AND entity_type = ?",
            (scope, EntityType.CHARACTER.value),
        ).fetchall()
        return {r["entity_name"] for r in rows}

    def _get_entity_state(self, scope: str, name: str) -> EntitySnapshot | None:
        """获取实体当前状态。"""
        row = self._store.connection.execute(
            "SELECT * FROM entity_states WHERE scope = ? AND entity_name = ?",
            (scope, name),
        ).fetchone()
        if row is None:
            return None
        return EntitySnapshot(
            entity_name=row["entity_name"],
            entity_type=EntityType(row["entity_type"]),
            status=LifecycleStatus(row["status"]),
            first_chapter=row["first_chapter"],
            last_chapter=row["last_chapter"],
            appearance_count=row["appearance_count"],
            current_holder=row["current_holder"],
            detail=json.loads(row["detail_json"]),
            updated_ts_ms=row["updated_ts_ms"],
        )

    def _get_exited_entities(self, scope: str) -> list[tuple[str, LifecycleStatus, int]]:
        """获取所有 EXITED/DESTROYED 实体。返回 [(name, status, last_chapter), ...]。"""
        rows = self._store.connection.execute(
            "SELECT entity_name, status, last_chapter FROM entity_states "
            "WHERE scope = ? AND status IN (?, ?)",
            (scope, LifecycleStatus.EXITED.value, LifecycleStatus.DESTROYED.value),
        ).fetchall()
        return [
            (r["entity_name"], LifecycleStatus(r["status"]), r["last_chapter"])
            for r in rows
        ]

    @staticmethod
    def _is_valid_transition(old: LifecycleStatus, new: LifecycleStatus) -> bool:
        """验证状态转换合法性（防回退）。"""
        valid_transitions = {
            LifecycleStatus.INTRODUCED: {LifecycleStatus.ACTIVE, LifecycleStatus.TRANSFORMED, LifecycleStatus.EXITED},
            LifecycleStatus.ACTIVE: {LifecycleStatus.DORMANT, LifecycleStatus.TRANSFORMED, LifecycleStatus.EXITED},
            LifecycleStatus.DORMANT: {LifecycleStatus.ACTIVE, LifecycleStatus.EXITED},
            LifecycleStatus.EXITED: {LifecycleStatus.REINTRODUCED},
            LifecycleStatus.REINTRODUCED: {LifecycleStatus.ACTIVE},
            LifecycleStatus.TRANSFORMED: {LifecycleStatus.ACTIVE},
            LifecycleStatus.ACQUIRED: {LifecycleStatus.HELD, LifecycleStatus.DESTROYED},
            LifecycleStatus.HELD: {LifecycleStatus.TRANSFERRED, LifecycleStatus.DESTROYED},
            LifecycleStatus.PLANTED: {LifecycleStatus.DEVELOPING, LifecycleStatus.ABANDONED},
            LifecycleStatus.DEVELOPING: {LifecycleStatus.CONVERGING, LifecycleStatus.RESOLVED, LifecycleStatus.ABANDONED},
            LifecycleStatus.CONVERGING: {LifecycleStatus.RESOLVED},
        }
        allowed = valid_transitions.get(old, set())
        return new in allowed

    def _update_appearance(
        self, scope: str, name: str, entity_type: EntityType, chapter: int
    ) -> None:
        """更新实体出场记录（last_chapter + appearance_count）。"""
        with self._store.lock:
            self._store.connection.execute(
                "UPDATE entity_states SET last_chapter = ?, appearance_count = appearance_count + 1, "
                "updated_ts_ms = ? WHERE scope = ? AND entity_name = ?",
                (chapter, int(time.time() * 1000), scope, name),
            )
            self._store.connection.commit()

    def _record_lifecycle_event(self, scope: str, event: LifecycleEvent) -> None:
        """写入不可变生命周期事件。"""
        with self._store.lock:
            self._store.connection.execute(
                "INSERT INTO lifecycle_events "
                "(scope, entity_name, entity_type, old_status, new_status, chapter, evidence, detail_json, ts_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    scope, event.entity_name, event.entity_type.value,
                    event.old_status.value if event.old_status else None,
                    event.new_status.value, event.chapter, event.evidence,
                    json.dumps(event.detail, ensure_ascii=False), event.ts_ms,
                ),
            )
            self._store.connection.commit()

    def _update_entity_state(self, scope: str, event: LifecycleEvent) -> None:
        """更新实体当前状态快照（UPSERT）。"""
        now_ms = int(time.time() * 1000)
        with self._store.lock:
            self._store.connection.execute(
                "INSERT INTO entity_states "
                "(scope, entity_name, entity_type, status, first_chapter, last_chapter, "
                "appearance_count, detail_json, updated_ts_ms) "
                "VALUES (?,?,?,?,?,?,1,?,?) "
                "ON CONFLICT(scope, entity_name) DO UPDATE SET "
                "status = excluded.status, last_chapter = excluded.last_chapter, "
                "appearance_count = entity_states.appearance_count + 1, "
                "detail_json = excluded.detail_json, updated_ts_ms = excluded.updated_ts_ms",
                (
                    scope, event.entity_name, event.entity_type.value,
                    event.new_status.value, event.chapter, event.chapter,
                    json.dumps(event.detail, ensure_ascii=False), now_ms,
                ),
            )
            self._store.connection.commit()

    def _save_style_profile(self, scope: str, profile: StyleProfile) -> None:
        """写入文笔特征（UPSERT by scope+chapter）。"""
        now_ms = int(time.time() * 1000)
        with self._store.lock:
            self._store.connection.execute(
                "INSERT INTO style_profiles (scope, chapter, profile_json, created_ts_ms) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(scope, chapter) DO UPDATE SET "
                "profile_json = excluded.profile_json, created_ts_ms = excluded.created_ts_ms",
                (
                    scope, profile.chapter,
                    profile.model_dump_json(), now_ms,
                ),
            )
            self._store.connection.commit()
