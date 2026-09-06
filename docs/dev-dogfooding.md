# dev_log 决策账本使用规范（Cortexa dogfooding，2026-09-06）

> 目标：让本仓库的「软件开发场景」成为实证场景（数据积累 → 足够后设计 code schema/S4）。

## 规则（三轻一重）

1. **决策即记（轻）**：任何不可逆/影响多文件的决策落一条 remember：
   `python examples/dev_log.py remember "<一句话决策+依据>"`；可加 --tag 分类（decision/lesson/architecture…）。
2. **口径修正用 retcon（轻）**：旧决策表述变更 → retcon（自动修正链，旧转 SUPERSEDED）。
3. **周复习 recall（轻）**：开工前 recall 相关决策：`dev_log.py recall "FSRS 实体通道 契约" -k 3`。
4. **月审计 audit/stats（重）**：`dev_log.py audit` 看 journal；`stats` 看孤岛率——
   孤岛 >60% 说明记了没复习（记而不用），需回查工作流。

## 数据与隔离

- 数据落 `~/.cortexa`（facade 默认），scope=`cortexa-dev`；与小说库（R2/.cortexa）与评测库天然隔离；
- 每周一次 `orphan_stats` 自查（journal API）；内容用中文一句话决策体。

## 本仓库已回填（示例，持续追加）

- 契约/合规/架构/机制/教训/设计 决策 10 条（2026-09-06 上午批次）+ 语义试点/embed 可用性 2 条（见 cortexa-dev scope recall 可查）。