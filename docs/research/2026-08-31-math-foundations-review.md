# 数学体系论证与 AI/人记忆差异核对（2026-08-31）

> 论证背景：用户提出三个问题——①数学体系（线性代数/矩阵/图论等）对本项目有何帮助；
> ②AI 记忆"文件永存"与人类"遗忘即真缺失"的本质差异如何纳入设计；③是否需要定时任务、
> 机器学习等整体增强能力。本文档基于 8 组网络检索 + 当前代码逐项核对，结论按评测驱动
> 原则给出行动分级。

## 一、数学体系逐学科核对表

| 数学工具 | 学科 | 记忆系统业务含义 | 当前项目状态 | 决策 | 依据 |
|---------|------|----------------|-------------|------|------|
| 余弦相似度 | 线性代数 | 语义召回的基础度量 | 已用（`vector_search`） | 保持 | — |
| Reciprocal Rank Fusion | 序统计 | 多通道排序无参融合 | 已用（`modules/search.py`） | 保持 | — |
| BM25 | 概率检索 | 精确通道词频相关性 | 已用（FTS5 内建） | 保持 | — |
| 特征哈希（随机投影思想） | 降维 | 离线确定性嵌入兜底 | 已用（`HashEmbedder`） | 保持为兜底层 | — |
| 幂律遗忘曲线 R(t)=(1+t/9S)^-1 | 动力系统 | 记忆可提取性随时间衰减 | `FSRSState` 已建模，R(t) 已接检索融合调制（`search._retention_factor`，首过中性零退化） | 已完成（本轮） | FSRS 实证公式（业务文档 8.1） |
| 双强度模型（storage/retrieval strength） | 认知心理学数学化 | 存储强度 vs 提取强度分离建模 | `retrieval_weight` 已进融合（R(t)×retrieval_weight，默认 1.0 中性） | 已完成（本轮） | Bjork 制约理论（见第二节） |
| 低秩近似 / SVD | 线性代数 | 向量库压缩、检索加速 | 未用 | **暂缓（规模触发器：>10 万条）** | MVP 单 scope 千级无需；YAGNI |
| Personalized PageRank / 随机游走 | 图论 | 关联传播、多跳召回 | `links` 字段已预留，无图算法 | **定向优化期决策** | HippoRAG（NeurIPS 2024，895 引用）：KG+PPR 模拟海马索引，多跳检索 SOTA |
| 社区检测（Louvain）/ 中心性 | 图论 | Wing→Room→Cluster 自动成簇 | wing/room 仅规则路由 | P2 拓扑地图时引入 | Graph-based Agent Memory 综述（arXiv 2602.05665） |
| 贝叶斯信念更新 | 概率论 | confidence 随证据演化 | confidence 静态 | P4 RARE 时引入 | Nous/Belief Engine（2026）：信念记忆有效但"条件性"——需诊断数据支撑 |
| KL 散度 / 熵 | 信息论 | 记忆簇冗余度、整合吸收判据 | 未用 | P3 Dreaming 时引入 | 整合吸收需要量化"多像才算冗余" |

## 二、AI 记忆 vs 人记忆差异：一个重要的学术修正

**用户直觉**：AI 记忆记录到文件除非删除否则永在；人类遗忘是信息真缺失。差异点需纳入设计。

**学术核对结果（Bjork 双强度理论）**：人类"遗忘"绝大多数是**提取失败（retrieval failure），
而非存储消失**。证据：舌尖现象（话到嘴边说不出）、再学习节省效应（重学比初学快）——
存储强度（storage strength）几乎不消失，衰减的是**提取强度（retrieval strength）**。
真正的存储丢失只发生在极端情况（海马体损伤、强干扰）。Nature Communications（2022）
在人工神经网络上的实验同样证明："遗忘"应该建模为可恢复的提取退化，而非数据删除。

**设计含义（本项目的核心差异化论点）**：
1. AI 文件永存 ≈ 存储强度无限——这不是缺陷，恰是对人类 storage strength 的正确类比；
2. AI 真正缺失的是**提取强度管理**：所有记忆在检索时永远同等待命，导致——
   旧信息与新信息无差别竞争排序（时序维度 C 的病根）、检索污染（precision 下降）；
3. 因此**遗忘引擎 = 提取强度衰减，不是物理删除**：
   - `retrieval_weight × R(t)` 进融合调制（W1 修复时预留的接线点：`storage.vector_search`
     docstring 注明"检索权重/可提取性的调制在融合层进行"）；
   - 物理删除仅限三种情况：官方合规（30 天清理）、用户主动、整合吸收（ARCHIVED 状态保留
     可追溯，不 DROP）；
   - RULE 记忆 `no_decay` 守卫不变（提取强度也不衰减——规则永远待命，这正是维度 F 需要）。

## 三、人类记忆强化三机制 → 系统对应

| 人类机制 | 心理学依据 | 系统机制 | 实现方式 | 排期 |
|---------|-----------|---------|---------|------|
| **回忆**（检索练习） | 测试效应（Roediger & Karpicke, Science 2008） | 检索命中即强化 | Search 命中后更新 `fsrs.last_review_at` + `consolidation_level += 1`，R(t) 恢复 | **W3（成本极低）** |
| **复述**（精炼重述） | 精细化复述（elaborative rehearsal） | RARE 检索增强重编码 | 检索后生成精炼记忆写回，`links` 指向原记忆 | P4 |
| **传播**（教授/泛化） | 学习迁移（protégé effect） | 事件→语义提升（核心记忆区） | 巩固等级达标（`consolidation_core_level=6`）后生成泛化语义记忆。**红线：泛化只在 scope 内**，跨 scope 违反官方隔离契约 | P3 巩固调度器 |

## 四、定时任务与机器学习论证

**定时任务 = Dreaming Cycle（离线通道），结论：需要，载体已明确**
- 实现载体：进程内 `asyncio` 后台任务（服务模式挂在 FastAPI lifespan；类库模式显式
  `cm.consolidate()`），空闲期执行：重放→整合→修剪→发现；
- 合规核对：官方硬规则只约束 ①Add/Search 内 LLM 必须 gpt-4o-mini ②Add 同步语义——
  空闲期整理不违反；但整理期间必须保证 Search 一致性（事务边界 + 只对 ACTIVE 生效）；
- 排期：W3 出设计稿并用本地模拟器回归，机制实现在 P3。

**机器学习，结论：现在不引入，作为定向优化期的可选项**
- 反方理由（当前）：①无训练数据——第 1 次 Full 的 badcase 还没产生，先验引入必然过拟合
  想象；②FSRS 是"用心理学实证公式替代数据拟合"的典范，MVP 够用；③排序器若用 LLM 实现
  受 gpt-4o-mini 红线约束（小排序器/非 LLM 模型不受限，但依然缺数据）；
- 正方条件（未来）：第 1 次 Full 拿到七维诊断 + 本地模拟器可回归后，Learning-to-Rank
  融合器（替代固定 RRF 权重）成为可验证的选项——**数据到位才做**。

## 五、结论与行动分级（评测驱动）

**立即做（W3，成本极低、直接作用于已证弱项）**：
1. FSRS 接线：检索命中 → `last_review_at`/`consolidation_level` 更新（测试效应落地）——**已完成**；
2. 融合调制：`score × retrieval_weight × R(t)`（提取强度管理落地）——**已完成**
   （`search._retention_factor`：factor=1+w·(R_eff-1)∈[1-w,1]，未复习/RULE/no_decay 恒中性，首次评测逐字节不变）；
3. 两者都必须过维度地板审计 + RULE no_decay 守卫回归——**已锁**（`test_review_fixes.TestFSRSRetentionModulation`）。

**W3 设计稿**：Dreaming Cycle 离线通道（asyncio 载体 + 一致性边界）——**已出稿**
（`docs/research/2026-08-31-dreaming-cycle-design.md`；本地模拟器+容器冒烟同批完成，机制实现仍属 P3）。

**定向优化期（10/11-10/24，按第 1 次 Full 七维诊断决策）**：
- 维度 B（多跳，63.4 分）丢分 → PageRank 关联传播（HippoRAG 路线，`links` 已预留）；
  前哨已落地（2026-08-31）：实体特征访问通道（`storage.entity_search`，实体倒排第三路进 RRF，
  `entity_channel` 可开关）。真库冒烟实测：实体标注覆盖率 1085/1737，但多为"Ch40"类结构性伪实体，
  中文专名抽取质量（P4）是通道收益的下一门槛；若 Full 诊断显示通道伤精度，开关置 False 即回双通道基线；
  **A/B 实测（2026-08-31 审计补记）**：内置题集与小说 1736 条库上开关两档覆盖率完全相同（Δ=0），
  且开启态延迟 15→54ms/题（扫描开销）——当前收益为零、成本为正，定位降为"NER 提质后的地基"；
- 维度 C（时序，60.0 分）丢分 → RRF 时间衰减因子；
- 维度 E/G 丢分 → 贝叶斯 confidence 更新（RARE 配套）。

**P2/P3 机制开发内置**：Louvain 成簇（拓扑地图）、KL 冗余判据（整合吸收）。

**规模触发器（记录在案，当前不做）**：向量库 SVD/PQ 压缩（>10 万条）、学习排序融合器
（诊断数据到位后）。

## 六、KV-Cache 理论核对（2026-08-31 全面 review 后补充）

**背景**：用户问“KV-Cache 理论对本项目有无帮助”。核对结论：**价值集中在
检索排序信号 + 存储经济性 + 冷热分层三处，不碰语义治理层**；盲目套用会把
可审计的离散治理退化成不可解释的软权重（扣维度 D 分）。

**本质同构**（两个共享假设）：①causal model 旧 token 的 K/V 一经计算不再重算，
只增不改 → 与本项目双时间轴 + supersede/retcon 链同一纪律（KV-Cache 给了它理论名分）；
②注意力 = 对全量 Key 做相似度 softmax 加权取值 = 一次软 kNN 检索 → 本项目
`vector_search` 是它的硬 top-k 近似，这解释了 precision-first 剪枝的必要性。

| 借用点 | 机制来源 | 映射到本项目 | 行动分级 |
|---|---|---|---|
| 分页向量缓存 | PagedAttention (vLLM) | `_append_vec_cache` 逐条 `np.vstack` 为冷启动摄取 O(N²) | **已修（本轮）**：分页累积，fold 一次合成；1500 条摄取 2.3s，对齐不变量测试锁 |
| 注意力质量当强化信号 | H2O/SnapKV heavy-hitter | `touch_retrieval` 现只记命中次数，丢相似度分 → 把融合分累加进 FSRS.stability | **W3**（R(t)×retrieval_weight 融合已完成，此行为其后续：把命中分数累加进 stability） |
| 可重算即可遗忘 | recompute-vs-cache 权衡 | 遗忘安全条件加一项：记忆在 link 图上高冗余（邻居可覆盖）→可衰减 | **P4 设计输入**（接已有 links 图，补强 config 两条纸面阈值） |
| 前缀共享 + 写时复制分叉 | Prompt Caching + CoW 页表 | 设定层=所有章节共享前缀；为分支剧情/A-B retcon 分叉 scope 仅复制改动页 | **backlog**（产品候选，非参赛必需） |
| 每 token 都有算力代价 | KV-Cache 经济性 | 为 `evidence_token_cap`/precision-first 截断提供经济依据 | **已做**（B-1 已将 budget flag 接入 /health 自证） |

**诚实边界（不 Cargo Cult）**：
1. KV-Cache 是无损精确记忆化，本项目是有损重构式记忆——它教访问模式经济性，
   不教语义层（实体归属/矛盾检测一点忙都帮不上）；
2. 不碰治理账本：retcon/supersede 必须保持离散可审计，只允许影响排序（H2O 信号）；
3. 向量量化（KIVI 式 int8）暂不急：先做分页解决 CPU 侧 O(N²)，RAM 矩阵触顶再引入（避免过早优化）。

## 附：证据源
- HippoRAG: Neurobiologically Inspired Long-Term Memory (arXiv:2405.14831, NeurIPS 2024)
- Graph-based Agent Memory: Taxonomy, Techniques (arXiv:2602.05665)
- Mem0 (arXiv:2504.19413)；A-Mem: Agentic Memory (NeurIPS 2025)
- Sleep-like unsupervised replay reduces catastrophic forgetting (Nature Communications, 2022)
- Bjork 双强度理论 / Retrieval Practice (Roediger & Karpicke, Science 2008)
- When Does Belief-Based Agent Memory Help? (arXiv:2606.22030)
- KV-Cache 侧：PagedAttention (vLLM, SOSP 2023)；H2O: Heavy-Hitter Oracle (NeurIPS 2023)；
  SnapKV (2024)；KIVI 量化缓存 (ICML 2024)；自动前缀缓存（vLLM APC / Anthropic prompt caching）
