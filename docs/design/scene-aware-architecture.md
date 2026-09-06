# Cortexa 场景化记忆系统 · 顶层架构设计（v2）

> 版本: v2.2 | 日期: 2026-09-04 | 状态: S1b/S2/S3 已实现（38 新增测试通过）
>
> 落地记录（2026-09-03/04）：
> - L2 domain/: cast.py(别名消解) / facts.py(语境分层) / lifecycle.py(状态机) / governance.py(AML治理)
> - L1 enhancer.py: 异步增强器 + pipeline 双阶段 Add 接入（daemon 线程方案 A）
> - L3 schema: novel(含 characters/aliases) + aml（USER 生命周期 + supersede/update 治理事件）
> - 真实库验证：周元白 ch41→DORMANT/ch105→REINTRODUCED（64章无交代回归自动命中）
> - novel_lifecycle_settle.py 消费 CastRegistry：元白/周元白 别名合并统计
> 上游: novel-adapter-architecture.md（v1.1 已实现） / llm-resilience-architecture.md（已实现）
> 触发: 图分析暴露架构缺口 + 比赛/小说/开发三类真实场景的统一诉求

## 一、设计命题

Cortexa 已在三个真实场景被使用，各自暴露同一组底层缺口：

| 场景 | 使用方式 | 暴露的缺口 |
|------|---------|-----------|
| AML 比赛 | Add 存评测数据 / Search 供平台答题 | 实体质量、关系稀疏、治理弱、异步增强缺失 |
| 小说创作 | 208 章摄取 / 写前 recall / 一致性核查 | 角色识别碎片、无交代离场、关系规则覆盖窄 |
| 软件开发 | remember/recall/retcon（宣称 dogfooding） | **推断场景非实证**：全库无代码决策账本的真实调用点（仅 novel_*.py 用 facade） |

> **场景实证分级（2026-09-03 Review 修正）**：
> - 小说、AML 为**实证场景**（有真实数据/真实使用）；
> - 软件开发为**推断场景**（README 宣称但无代码使用点）——S4 前不得为它设计 schema，
>   待真实使用出现（如本仓库自身以 Cortexa 记录决策）后再提炼。

**核心命题**：三场景共享同一记忆内核（Add/Search/存储/巩固），差异在"领域理解层"——
不同场景对"什么算实体、什么算事件、什么算生命周期、什么算关系"有不同语义。
当前这些差异散落在 novel_*.py 脚本与规则正则里，无法复用、无法演进、无法被比赛消费。

## 二、设计原则（承接 v1）

1. **内核场景无关**：Add/Search/存储/FSRS/RARE/LLM 韧性——任何场景相同，不写场景 if。
2. **场景 = 声明式 Schema**：实体类型/事件谓词/关系类型/生命周期状态机 = 数据，不是代码。
3. **写入期增强，读取期克制**：LLM 实体消歧/关系推断等重活在 Add 异步阶段做，Search 保持预算内纯检索。
4. **分析读库，不重算**：graph/checkup/backfill 消费库中结构化数据，不重扫原文。
5. **主动≠擅自**：自动发现进 PROVISIONAL；低置信关系不进正式图；死亡只认 NARRATION 语境。
6. **真实场景反馈闭环**：每个场景的使用产物（检索日志/审计报告/一致性异常）回流为能力增强依据。

## 三、三层架构

```
┌────────────────────────────────────────────────────────┐
│ L3 场景层 Scenario Layer（声明式 Schema，可插拔）           │
│  novel.schema   code.schema   aml.schema   custom        │
│  定义: 实体类型/事件谓词/关系类型/生命周期状态机/死亡与状态词表  │
├────────────────────────────────────────────────────────┤
│ L2 领域模型层 Domain Model（通用抽象，场景无关）             │
│  CastRegistry 角色/对象注册 + 别名消解                     │
│  FactExtractor 结构化事件: 谓词+主体+客体+语境(NARRATION/   │
│    QUOTE/RECALL/REPORT)+置信度                            │
│  LifecycleEngine 状态机: INTRODUCED/ACTIVE/DORMANT/       │
│    EXITED/REINTRODUCED + 自动离场判定                      │
│  RelationInference 关系证据链(显式/语义/共现→置信度分级)      │
├────────────────────────────────────────────────────────┤
│ L1 记忆内核 Memory Core（v1 已实现，不变）                  │
│  Add(同步核心+异步增强) / Search / SQLite+FTS+向量          │
│  FSRS 巩固 / RARE-lite / LLM 韧性 / retcon / journal      │
└────────────────────────────────────────────────────────┘
```

### 三层职责边界

| 层 | 拥有 | 禁止 |
|----|------|------|
| L3 场景 | schema 定义、场景脚本编排、报告呈现 | import 内核内部、写库逻辑 |
| L2 领域 | 通用抽取/状态机/消解算法 | 场景专属词表硬编码（应读 schema） |
| L1 内核 | 存取/检索/巩固/事务 | 任何场景语义判断 |

## 四、L3 场景 Schema 设计

```yaml
# scene: 一个场景 = 一份 schema 文件（.yml），运行时可加载
scene: novel
entity_types: [CHARACTER, ITEM, FACTION, LOCATION, STORY_THREAD]
lifecycle_states:
  CHARACTER: [INTRODUCED, ACTIVE, DORMANT, EXITED, REINTRODUCED, TRANSFORMED]
  ITEM:       [ACQUIRED, HELD, TRANSFERRED, DESTROYED]
event_predicates:      # 场景能识别的事件谓词
  death:    [死了, 身亡, 陨落, 被杀, 灭口, 战死]
  depart:   [离开, 失踪, 下落不明, 出城, 远走]
  return_:   [回来, 归来, 重逢, 现身]
  transform: [突破, 晋升, 觉醒, 黑化, 入魔]
relation_types: [师徒, 道侣, 好友, 亲子, 仇敌, 同门, 主从]
name_stopwords: [的时候, 来了, 出来, 东西, ...]
dormant_threshold_ch: 10
```

**事件→状态迁移表（2026-09-03 Review 修正）**：schema 必须显式声明"哪个事件把实体
从哪个状态迁到哪个状态"——否则图分析暴露的"死亡 vs 离场歧义"（周元白/王宣案例）
会在实现期重现。

```yaml
# schema 增加：事件→状态迁移（L2 LifecycleEngine 消费）
transitions:
  # 事件: {from 允许的当前状态, to 目标状态}
  death:    {from: [INTRODUCED, ACTIVE, DORMANT], to: EXITED}
  depart:   {from: [ACTIVE], to: DORMANT}          # 离场≠死亡
  return_:  {from: [DORMANT], to: REINTRODUCED}    # 回归需显式标记
  transform: {from: [INTRODUCED, ACTIVE], to: TRANSFORMED}
  # 语境约束：仅 NARRATION 语境的事件可驱动状态迁移（QUOTE/RECALL 不迁移）
context_required: [death]
```

小说/代码/比赛三份 schema 的差异只在 L3：

| 维度 | novel | code | aml |
|------|-------|------|-----|
| 实体 | 角色/物品/势力 | 服务/模块/API/配置 | 用户/实体/偏好 |
| 事件 | 突破/死亡/离场 | commit/重构/废弃 | 状态更新/偏好变更 |
| 关系 | 师徒/道侣/仇敌 | 依赖/调用/继承 | 拥有/偏好/关联 |
| 生命周期 | 出场→离场→回归 | 引入→活跃→废弃 | 新增→更新→修正 |

## 五、L1 写入架构：双阶段 Add（跨场景统一）

### 5.1 同步核心（红线内，纯确定性）

```
Add req ──> [去重] ──> [规则 classify] ──> [encode] ──> [dimension_floor]
        ──> [embed(hash/openai)] ──> [store] ──> 返回 success (立即可检索)
         全程 < 预算(500ms)，零 LLM 或 LLM 带紧超时兜底
```

### 5.2 异步增强（返回后执行，不阻塞）

```
Add 返回后 ──> 增强队列 (每 request_id 幂等)
  ├─ EntityResolver  LLM 实体消歧 → 规范化实体名 → 回写 entities
  ├─ FactExtractor   事件结构化 → 写 lifecycle_events + entity_states
  ├─ RelationInfer   关系补边 → 写 relations(带证据+置信度)
  └─ 巩固调度       FSRS 参数微调（如需）
增强失败 = 记忆保持同步核心质量，不阻塞、不重试爆炸（韧性层）
```

### 5.3 合规论证

- 官方红线："Add 返回成功前消息必须全部存好且可被 Search"——异步增强在返回后执行，不违反
- 增强只补充记忆的组织维度（entities/links/结构化事件），不改 content 原文（维度 A 防漂移不变）
- **增强开关独立于 journal_mode（2026-09-03 Review 修正）**：新增 `async_enhance_enabled`（默认 true）。
  journal_mode=eval 只管日志脱敏，不管增强开关——比赛恰恰需要小说验证出的实体/关系能力。
  评测预算控制用独立的 `enhance_budget_ms`/`enhance_llm_cap`，不混用日志语义。

## 五·五、S0.5：异步增强执行载体（Review 偏差 2 的补设计）

当前 server.py 的 store 是闭包单例、无队列、Add 全同步——"返回后异步增强"无承载机制。
三个候选载体（S1 先落 A 最小版，S3 前评估是否需 B）：

| 方案 | 机制 | 适用 | 局限 |
|------|------|------|------|
| A. 进程内线程池 | Add 响应后 spawn 后台线程；按 request_id+memory_id 幂等 | 单机、数据量小 | 评测进程退出即丢未完成增强 |
| B. 惰性命中触发 | Search 命中未增强记忆时触发（写读分离窗口） | 比赛（Search 密集） | Search 200ms 红线内成本需评估 |
| C. 显式批处理 | 平台 Add 批完后、Search 前调 /enhance | 需 AML 流程确认 | 契约未证实有该窗口 |

**幂等保证**：每 memory 增补一次（memory.metadata.enhanced=true 防重）；
**失败语义**：增强失败 = 记忆保持同步核心质量，记 journal 不重试爆炸。

## 六、L2 领域模型与现有代码映射

| 迭代设计 | 落点 | 复用现有 | 新增 |
|---------|------|---------|------|
| Cast Registry | cortexa/domain/cast.py | entity_states 表 | aliases 表 + PROVISIONAL 状态 |
| Fact Extractor | cortexa/domain/facts.py | novel_adapter 关键词 | 语境分层(NARRATION等) + 证据链 |
| Lifecycle Engine | cortexa/domain/lifecycle.py | lifecycle_events 表 + 状态机 | DORMANT 自动结算 + REINTRODUCED 警告 |
| Relation Inference | cortexa/domain/relations.py | graph.py + infer_relations | 置信度分级 + 证据链累积 |

## 七、真实场景反馈闭环（设计的第 6 原则落地）

```
场景使用 ──> 留痕(journal/search日志/audit报告) ──> 体检(checkup/consistency/graph)
     │                                                    │
     └──── 异常/缺口清单 (孤岛/误报/无交代离场/关系稀疏) ────┘
                          │
                          ▼
        能力增强依据: 扩充 schema 词表 / 调阈值 / 增规则 / 加模式
                          │
                          ▼
                    (回到场景使用，循环)
```

**验证锚点**（每轮迭代用真实案例验收）:
- 周元白 64 章无交代离场 → LifecycleEngine 应自动报 DORMANT+REINTRODUCED(无交代)
- 王宣/方桐死后引用 → FactExtractor 语境分层应识别为 RECALL/QUOTE 而非 EXITED
- 比赛评测 → Search 召回质量（实体规范化后多跳命中率）

## 八、演进路线

| 阶段 | 内容 | 依赖 |
|------|------|------|
| S0.5 载体选型 | 异步增强执行载体（先落方案 A 线程池） | v1 全量 + 本设计 |
| S1 内核稳定 | 双阶段 Add 框架 + schema 加载器 | S0.5 |
| S2 novel schema | 小说场景 schema + LifecycleEngine + backfill | S1 |
| S3 aml schema | 比赛场景 schema + 写入期治理检测 | S1 |
| S4 code schema | 开发场景 schema（模块/API/依赖生命周期） | S1 |
| S5 反馈自动化 | checkup 异常 → schema 建议（半自动） | S2-S4 |

## 八·五、治理漂移修复记录（2026-09-04）

复盘发现并修复两个真实缺陷（均有测试锁死，tests/test_governance_e2e.py）：

1. **评测场景漂移**：examples/local_eval.py 是 AML 评测却用默认 scene="novel"——
   治理引擎拿 novel schema（中文死亡/离开事件词）治理英文偏好，AML 维度 D 的
   supersede/update/withdraw 治理词全部空转。修复：评测显式 scene="aml"。
2. **治理过度（防误伤红线）**：GovernanceEngine.should_supersede 原实现见治理词即杀
   同 subject 记忆——实测 "Alice currently prefers coffee" 误杀了无关的
   "Alice adopted a dog named Rob"（同 subject 不同属性）。修复：词信号命中后
   必须再有"内容同主题"证据（子串包含 / 共享内容词 / 共享非主体实体）才 supersede。

修复后全量 229 测试绿；local_eval 基线不变（hit@1 0.909 / hit@5 1.0 / MRR 0.955），
证明治理修复不伤检索精度。

3. **治理纳入评测（维度 D 可评分）**：治理此前只是"不干扰评测"，现升级为 local_eval
   的被评测维度（examples/local_eval.py 治理评测段）：3 正向 supersede 用例
   （currently / no longer / now+prefer over）+ 1 防过度对照，判据为**状态级硬验证**
   （直连 db：旧记忆 status=SUPERSEDED 且 superseded_by 指向新 id）+ 检索闭环
   （新记忆可被 Search）。独立 scope（gov-*）不污染 eval-local 检索基线。
   全绿锁在 tests/test_local_eval.py::test_local_eval_governance_dimension_full_green。

4. **维度 B/C/F 纳入 local_eval（2026-09-04）**：评测从"A 单事实 + D 治理"扩到
   B/C/F 三观测列（tests/test_local_eval.py 全绿锁）：
   - dim_B = 线索齐备：同实体多条记忆须全部被检索带回（平台 top-K 拼答案的必要
     条件，非推理本身——本地 hash 嵌入无语义，多跳推理判答留给官方）；
   - dim_C = 时序召回：正确时间点/最新事实可检索；
   - dim_F = 规则可见：RULE 记忆 no_decay + rule_boost 的直接验证。
   全部词元确定性命中（2/2/2 全绿），基线 hit@1/MRR 不回退。
   诚实边界：B 是"检索侧必要条件"代理指标，不是多跳推理分。

5. **维度 G precision 修复链（2026-09-04，七维诊断预演暴露）**：aml_diagnostic.py
   预演实测 G（epistemic safety）0/3——不可答问题返回弱相关噪声。三处修复：
   - storage._fts_escape 加英文停用词过滤（_FTS_EN_STOP）：高频虚词 IDF≈0 使弱匹配
     bm25=0 与强匹配无法区分，OR MATCH 裸奔；
   - Settings.vector_min_cos_hash=0.28（hash 专用语义下限）：hash 词袋余弦=词重叠
     比例，语义嵌入的 0.05 阈值对 hash 过松（实测无关对 0.03-0.28、相关对 0.38+）；
     provider 自适应选择（openai 仍用 0.05）；
   - vector_search RULE/no_decay 豁免剪枝：规则召回优先于 precision 代价（维度 F
     红线"宁多收进 Wing"）——避免 0.28 阈值误伤弱词元规则查询。
   验证：G 0/3 → 3/3，A 基线 0.909/0.955 不变，F 保持 2/2。
   配套：examples/aml_diagnostic.py 七维诊断预演（A-G 对照表，9/20 报名周前自评）。

## 九、风险与边界

| 风险 | 缓解 |
|------|------|
| 过度抽象（schema 万能化） | 先落 novel+aml 两个真实 schema，code 后补；拒绝为抽象而抽象 |
| 异步增强与同步库竞争 | 增强仅写组织维度字段，主事务锁已有（store.lock） |
| 评测预算失控 | eval 模式降级纯规则 + 增强队列预算上限 |
| schema 与代码漂移 | schema 加载时校验（缺字段即报错），单测覆盖 |

---
> 本设计将 v1 的 novel_adapter 从"网文专属模块"升级为"L2 通用领域模型 + L3 场景 schema"，
> 使小说验证的能力能被比赛与开发场景直接复用。

---
## 附：novel_backfill 职责边界实证（2026-09-04）

DB 记忆的 entities_json 由英文实体抽取器产出，对中文产出引号碎片/标点（实锤：
"叶长生。"、"对。"、"渊"），**不可作为角色在场数据源**。novel_backfill.py 重构为
Cast 名单文本在场检测（扫 DB 记忆文本匹配 schema.characters+aliases），注册 22
角色全绿、噪声归零（原 304 噪声 → 12 → 22 真角色）。

职责分工（互补不重叠）：
- backfill = 出场注册/统计（entity_states 主名注册面，DB 片段流够用）；
- lifecycle_settle = 死亡/离场精确状态判定（完整章节文件 + NARRATION 语境，
  片段流缺语境且 chsettings 设定表含"已死"字样会污染）。
