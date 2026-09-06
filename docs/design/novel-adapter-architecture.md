# 网文领域适配器 + 生命周期管理 架构设计

> 版本: v1.1 | 日期: 2026-09-03 | 状态: 已实现（P0 回填 / P1 验证 / P2 RARE-lite / P3 关系推断）
>
> 实现记录（2026-09-03）：
> - P0 存量回填：examples/novel_backfill.py（确定性本地，零 LLM，--force 全量重建，--report 健康度快照）
> - P2 RARE-lite：检索命中记忆两两关联写回（storage.add_links + adapter.rare_strengthen，search 自动挂接，config.rare_enabled）
> - P3 关系推断：adapter.infer_relations（命名组捕获 A/B，防幻觉：关系双方须为已注册实体；pipeline 挂接，config.relation_inferrer 默认关）

## 一、问题陈述

Cortexa 当前是通用记忆系统，在网文实战中暴露三个结构性缺口：

1. **实体抽取质量差**：启发式正则对中文对话体失效，Top跨章实体是对话碎片而非角色名
2. **无生命周期管理**：不知道角色是"暂时离场"还是"永久下线"，无法预防"下线角色复活"幻觉
3. **文笔记忆空白**：2,620条记忆中仅3.9%含风格信息，且全是设定文档元描述

根因：缺少一个理解"什么是网文中的重要信息"的**领域感知层**。

## 二、设计原则

### 2.1 主动 ≠ 擅自

| 人类"主动学习" | 系统对应 | 防幻觉红线 |
|---------------|---------|-----------|
| 读完一章后总结人物状态变化 | 摄取后结构化提取 | 只从原文提取事实，不推断未写明内容 |
| 注意到文风变化 | 文笔特征持续追踪 | 统计特征（句长/对话比），不做主观评价 |
| 把新角色和已知角色关联 | 实体消歧+关系推断 | 只在原文明确表述时建边 |
| 回忆角色上次出场 | 生命周期时间线索引 | 基于session_id的确定性排序 |

### 2.2 高内聚低耦合

- **适配器是独立模块**，不修改现有管线核心逻辑
- **通过钩子(hook)集成**，pipeline/search 只增加调用点，不改内部
- **领域无关内核不变**，adapter 通过 `Settings.domain` 切换
- **存储扩展向后兼容**，新表与旧表同库同锁，旧代码不受影响

### 2.3 渐进式落地

每个子模块可独立启用/禁用，不形成全有或全无的依赖链。

## 三、架构总览

```
┌─────────────────────────────────────────────────────────┐
│                    Cortexa Core (不变)                    │
│  pipeline.run_add ──► classify → encode → floor → embed  │
│  search.search_memory ──► FTS + vector → RRF → boost    │
│  storage.MemoryStore ──► SQLite + FTS5 + numpy           │
│  models.MemoryInstance / AddRequest / SearchResult       │
└──────────────┬──────────────────────┬────────────────────┘
               │ hook                 │ hook
               ▼                      ▼
┌─────────────────────────────────────────────────────────┐
│              NovelAdapter (新增，独立模块)                  │
│                                                         │
│  post_ingest hooks:                pre_return hooks:     │
│  ├─ EntityResolver                 └─ LifecycleGuard    │
│  ├─ CharacterTracker                                    │
│  ├─ StyleProfiler                                       │
│  └─ RelationInferrer                                    │
│                                                         │
│  存储: entity_states + lifecycle_events (新表)            │
│  配置: Settings.novel_adapter_enabled + 子开关             │
└─────────────────────────────────────────────────────────┘
```

## 四、数据结构设计

### 4.1 新增枚举 (models.py)

```python
class EntityType(str, Enum):
    """业务对象类型。domain=code 时映射为 SERVICE/API/CONFIG/EPIC。"""
    CHARACTER = "CHARACTER"
    ITEM = "ITEM"
    FACTION = "FACTION"
    LOCATION = "LOCATION"
    STORY_THREAD = "STORY_THREAD"

class LifecycleStatus(str, Enum):
    """统一生命周期状态。不同 EntityType 使用不同子集。"""
    # 角色: INTRODUCED → ACTIVE → DORMANT → EXITED / REINTRODUCED / TRANSFORMED
    # 物品: ACQUIRED → HELD → TRANSFERRED → DESTROYED
    # 故事线: PLANTED → DEVELOPING → CONVERGING → RESOLVED / ABANDONED
    INTRODUCED = "INTRODUCED"
    ACTIVE = "ACTIVE"
    DORMANT = "DORMANT"
    EXITED = "EXITED"
    REINTRODUCED = "REINTRODUCED"
    TRANSFORMED = "TRANSFORMED"
    ACQUIRED = "ACQUIRED"
    HELD = "HELD"
    TRANSFERRED = "TRANSFERRED"
    DESTROYED = "DESTROYED"
    PLANTED = "PLANTED"
    DEVELOPING = "DEVELOPING"
    CONVERGING = "CONVERGING"
    RESOLVED = "RESOLVED"
    ABANDONED = "ABANDONED"
```

### 4.2 新增模型 (models.py)

```python
class LifecycleEvent(BaseModel):
    """生命周期事件（不可变记录，类似 Git commit）。
    
    防幻觉红线：每条事件必须有 evidence（原文片段）+ chapter（来源章节）。
    无证据的状态转换一律拒绝。
    """
    entity_name: str
    entity_type: EntityType
    old_status: LifecycleStatus | None = None
    new_status: LifecycleStatus
    chapter: int
    evidence: str                    # 原文片段（截断至200字）
    detail: dict[str, Any] = {}      # 附加信息（修为变化/持有者变更等）
    ts_ms: int = Field(default_factory=lambda: int(time.time() * 1000))

class EntitySnapshot(BaseModel):
    """实体当前状态快照（entity_states 表的 Pydantic 映射）。"""
    entity_name: str
    entity_type: EntityType
    status: LifecycleStatus
    first_chapter: int
    last_chapter: int
    appearance_count: int = 0
    current_holder: str | None = None   # 物品持有者
    detail: dict[str, Any] = {}
    updated_ts_ms: int = 0

class StyleProfile(BaseModel):
    """单章文笔特征（统计量，非主观评价）。"""
    chapter: int
    avg_sentence_len: float
    dialogue_ratio: float             # 对话行数 / 总行数
    paragraph_avg_len: float
    rhetoric_density: float           # 修辞词数 / 总字数
    pov: str                          # third_limited / first / omniscient
    sample_sentences: list[str] = []  # 代表性句式（最多5条）
```

### 4.3 新增数据库表 (storage.py _SCHEMA 扩展)

```sql
-- 实体生命周期当前状态（快速查询，每实体一行）
CREATE TABLE IF NOT EXISTS entity_states (
    scope TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    status TEXT NOT NULL,
    first_chapter INTEGER NOT NULL,
    last_chapter INTEGER NOT NULL,
    appearance_count INTEGER NOT NULL DEFAULT 0,
    current_holder TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    updated_ts_ms INTEGER NOT NULL,
    PRIMARY KEY (scope, entity_name)
);

-- 生命周期事件历史（不可变审计链，只追加不修改）
CREATE TABLE IF NOT EXISTS lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    chapter INTEGER NOT NULL,
    evidence TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    ts_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_entity
    ON lifecycle_events(scope, entity_name, chapter);

-- 文笔特征时序（每章一行）
CREATE TABLE IF NOT EXISTS style_profiles (
    scope TEXT NOT NULL,
    chapter INTEGER NOT NULL,
    profile_json TEXT NOT NULL,
    created_ts_ms INTEGER NOT NULL,
    PRIMARY KEY (scope, chapter)
);
```

## 五、模块接口设计

### 5.1 NovelAdapter 统一入口

```python
class NovelAdapter:
    """网文领域适配器。所有方法纯函数式，不持有状态。
    
    构造时注入依赖（store/llm/settings），不创建自己的连接。
    与现有管线共享同一 store.lock，保证事务一致性。
    """
    
    def __init__(self, store: MemoryStore, llm: LLMClient, settings: Settings): ...
    
    # --- post_ingest hooks (Add管线末尾调用) ---
    
    def resolve_entities(
        self, text: str, chapter: int, scope: str
    ) -> list[ResolvedEntity]:
        """LLM实体抽取+消歧。替代 encoding.extract_entities。
        
        Returns: 消歧后的实体列表，每个带 entity_type + confidence。
        失败时降级返回空列表（不阻断管线）。
        """
    
    def track_characters(
        self, memories: list[MemoryInstance], chapter: int, scope: str
    ) -> list[LifecycleEvent]:
        """从本章记忆中推断角色状态变化。
        
        只做确定性推断（原文明确写了"突破""死亡""离开"等）。
        模糊情况标记 confidence<0.6，不入 lifecycle_events。
        """
    
    def profile_style(
        self, text: str, chapter: int, scope: str
    ) -> StyleProfile | None:
        """计算单章文笔统计特征。纯本地计算，零LLM。"""
    
    def infer_relations(
        self, entities: list[ResolvedEntity], text: str, chapter: int, scope: str
    ) -> list[dict]:
        """从原文提取显式关系表述，生成建边请求。
        
        只处理明确表述（"拜为师""结为道侣"），不从共现推断。
        返回 graph.add_link 的参数列表，由调用方执行。
        """
    
    # --- pre_return hook (Search管线返回前调用) ---
    
    def guard_lifecycle(
        self, results: list[SearchResult], scope: str
    ) -> list[SearchResult]:
        """检查返回结果中是否涉及 EXITED/DESTROYED 实体。
        
        如有，在 SearchResult.memory 文本末尾追加警告标记。
        不删除结果（precision-first：给写作系统判断权）。
        """
```

### 5.2 与现有管线的集成契约

**pipeline.py run_add 变更（~15行）**：

```python
def run_add(store, llm, settings, req, journal=None):
    # ... 现有管线不变 ...
    
    # [新增] post_ingest hook
    if settings.novel_adapter_enabled and pending:
        adapter = NovelAdapter(store, llm, settings)
        chapter = _extract_chapter(req.session_id)
        full_text = "\n".join(m.content for m in pending)
        
        # 1. 实体消歧（可选替换 encoding 阶段的启发式结果）
        if settings.adapter_entity_resolver:
            resolved = adapter.resolve_entities(full_text, chapter, req.scope)
            # 用消歧结果更新 pending 记忆的 entities 字段
        
        # 2. 角色生命周期追踪
        if settings.adapter_character_tracker:
            events = adapter.track_characters(pending, chapter, req.scope)
            # 写入 lifecycle_events + 更新 entity_states
        
        # 3. 文笔特征
        if settings.adapter_style_profiler and chapter is not None:
            profile = adapter.profile_style(full_text, chapter, req.scope)
            # 写入 style_profiles
        
        # 4. 关系推断
        if settings.adapter_relation_inferrer:
            new_links = adapter.infer_relations(resolved, full_text, chapter, req.scope)
            # 调用 graph.add_link
    
    return AddResponse(...)
```

**search.py search_memory 变更（~8行）**：

```python
def search_memory(store, llm, settings, *, query, scope, top_k, journal=None):
    # ... 现有检索+融合不变 ...
    
    results = [to_search_result(mem, score) for mem, score in scored[:k]]
    
    # [新增] pre_return hook
    if settings.novel_adapter_enabled and settings.adapter_lifecycle_guard:
        adapter = NovelAdapter(store, llm, settings)
        results = adapter.guard_lifecycle(results, scope)
    
    # touch_retrieval + journal（不变）
    ...
    return results
```

### 5.3 Settings 扩展 (config.py)

```python
# ---- 网文适配器 ----
novel_adapter_enabled: bool = False       # 总开关
adapter_entity_resolver: bool = True      # LLM实体消歧
adapter_character_tracker: bool = True    # 角色生命周期
adapter_style_profiler: bool = True       # 文笔特征
adapter_relation_inferrer: bool = False   # 关系推断（P2）
adapter_lifecycle_guard: bool = True      # 检索预警
adapter_dormant_threshold: int = 10       # >N章未出现 → DORMANT
```

## 六、生命周期状态机规则

### 6.1 角色状态转换矩阵

| 当前状态 | 可转换到 | 触发条件 | 证据要求 |
|---------|---------|---------|---------|
| (新建) | INTRODUCED | 首次出现在正文中 | 章节号 + 原文提及 |
| INTRODUCED | ACTIVE | 连续2+章出现 | 自动 |
| ACTIVE | DORMANT | >dormant_threshold章未出现 | 自动（checkup触发） |
| ACTIVE | TRANSFORMED | 原文明确写突破/觉醒/黑化 | 原文片段 |
| ACTIVE | EXITED | 原文明确写死亡/永久离开 | 原文片段 |
| DORMANT | ACTIVE | 重新出现 | 章节号 + 原文提及 |
| DORMANT | EXITED | 确认退场 | 原文片段 |
| EXITED | REINTRODUCED | 明确回归（需人工确认） | 原文片段 + reason |

### 6.2 防幻觉硬约束

1. **无证据不转换**：所有非自动转换必须携带 `evidence` 字段
2. **EXITED 不可逆**：EXITED → REINTRODUCED 需要显式 reason，journal 留痕
3. **状态单调性**：不允许 ACTIVE → INTRODUCED 等回退
4. **置信度门槛**：LLM推断的转换 confidence < 0.6 时标记 PROVISIONAL，不参与 guard

## 七、IT ↔ 网文 领域映射表

适配器通过 `Settings.domain` 切换行为。以下是两个领域的概念对齐：

| 抽象概念 | domain=novel | domain=code |
|---------|-------------|------------|
| 业务对象 | Character/Item/Faction | Service/API/Config |
| 生命周期 | INTRODUCED→ACTIVE→EXITED | DEPLOYED→ACTIVE→DEPRECATED |
| 状态变更证据 | 正文章节 | Git commit / PR |
| 关系 | 师徒/敌友/传承 | 依赖/调用/继承 |
| 故事线 | 主线/支线/暗线 | Epic/Feature/Sprint |
| 文笔特征 | 句长/对话比/修辞密度 | 代码复杂度/测试覆盖/注释率 |
| 里程碑 | 突破/大战/揭秘 | Release/Migration/Launch |
| 冲突解决 | retcon | git revert/cherry-pick |
| 废弃预警 | EXITED角色被引用 | DEPRECATED API被调用 |

## 八、实现优先级与验收标准

| 优先级 | 模块 | 验收标准 | 预估工作量 |
|-------|------|---------|-----------|
| P0 | EntityResolver | 叶长生进入Top5跨章实体；对话碎片不再占据Top10 | 2h |
| P0 | CharacterTracker + lifecycle表 | 能回答"叶长生当前状态"；EXITED角色检索时附带警告 | 3h |
| P1 | LifecycleGuard | 搜索"苏晚晴"时若已EXITED则结果带⚠️标记 | 1h |
| P1 | StyleProfiler | 每章生成StyleProfile；能回答"最近10章文风趋势" | 2h |
| P2 | RelationInferrer | 图谱边数从8增长到50+；能回答"叶长生的人际关系网" | 3h |
| P3 | domain=code 适配 | 同一套状态机驱动代码项目生命周期 | 4h |

## 九、风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| LLM实体抽取增加Add延迟 | 违反500ms预算 | 批量抽取(整章一次)；hash embedding时跳过 |
| 生命周期误判(假EXITED) | 错误预警干扰写作 | confidence门槛 + PROVISIONAL标记 + 人工覆写接口 |
| 新表写入与主表不一致 | 状态漂移 | 同事务提交；checkup定期校验一致性 |
| 文笔统计对短章不准 | 噪声数据 | 字数<500的章节跳过profiling |
