# Dreaming Cycle 设计稿（W3 出稿，P3 实现）

日期：2026-08-31。依据：研究文档第四节（定时任务论证）、计划文件 W3/P3 排期。
本稿只做设计锚定，不引入任何代码——机制实现属 P3，且必须先过本地评测模拟器回归。

## 一、定位

空闲期离线通道：重放 → 整合 → 修剪 → 发现。与在线主管线（Add 同步写入 / Search
精度优先）完全解耦——官方硬规则只约束 Add/Search 内行为，空闲期整理合规。

- 载体：进程内 `asyncio` 后台任务。服务模式挂在 [server.py](../../cortexa/server.py)
  既有 `lifespan`（当前只做 `store.close()`，挂点是现成的）；类库模式显式
  `cm.consolidate()`（与 `forget_scope` 同层，显式触发不隐式后台）。
- 触发：空闲期（无进行中的 Add/Search 请求窗口）；不引入系统级定时器依赖。

## 二、四阶段语义

| 阶段 | 输入 | 动作 | 状态迁移 | 红线 |
|------|------|------|----------|------|
| 重放 | journal 检索事件 | 统计高频命中记忆，喂巩固 | `consolidation_level` 已有检索侧 +1（`touch_retrieval`），此阶段只读不写 | 评测模式（journal_mode=eval）内容已脱敏，重放只认计数 |
| 整合 | 同 scope 高相似记忆对 | 冗余吸收：保留代表、合并链接 | ACTIVE → ARCHIVED（被吸收方，保留可追溯不 DROP） | KL 判据（研究文档表：P3 引入）定"多像才算冗余"；无判据不动手 |
| 修剪 | 长期低提取强度记忆 | 衰减遗忘（非物理删除） | ACTIVE → FORGOTTEN | RULE / `no_decay` 强制豁免；只动提取强度，存储永在（Bjork 双强度：人类遗忘=提取失败） |
| 发现 | 巩固等级达标记忆 | 事件 → 语义泛化提升 | 新生语义记忆，`links` 指向源 | **泛化只在 scope 内**——跨 scope 违反官方隔离契约（研究文档第三节红线） |

## 三、一致性边界（Search 不受扰）

1. 事务边界：每个阶段独立短事务，持 `storage._lock`（RLock 已就位）；
   不允许跨阶段长事务——Search 随时可插入。
2. 只对 ACTIVE 生效：`entity_search`/`fts_search`/`vector_search` 均已按
   status='ACTIVE' 过滤，迁移即退出召回面，无需改检索。
3. 状态迁移单向：ACTIVE → ARCHIVED/FORGOTTEN 后不回跳；误吸收的救济入口是
   管理操作（P3 提供），不由 Dreaming 自动回滚。
4. 零新外部依赖：SQLite WAL + 现有连接，不引 Redis/队列（规模触发器外）。

## 四、验收口径（P3 实现时）

- 本地评测模拟器（`examples/local_eval.py`）Dreaming 前后 Hit@5 不回退——
  模拟器已是回归裁判（硬下限 0.9，`tests/test_local_eval.py` 锁定）。
- 维度地板审计复跑：RULE 永不衰减、scope 隔离 100%。
- 新增开关：`Settings.dreaming_enabled`（默认 False，P3 才翻默认值；
  与 `entity_channel` 同款"开关置 False 即回基线"回退纪律）。

## 五、明确不做（防过度设计）

- 不做跨进程分布式调度（单进程 + asyncio 够用，平台 Docker 单容器部署）。
- 不做"睡眠周期"拟真时序（间隔参数化即可，不模拟生物节律）。
- 整合阶段不做 LLM 重写摘要（合规红线：离线通道也不擅换模型；
  如需 LLM 参与整合，只能 gpt-4o-mini 且计入 token 预算——P3 再决策）。
