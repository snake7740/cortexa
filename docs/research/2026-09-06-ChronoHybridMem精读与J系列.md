# ChronoHybridMem 精读：上届文章系统的完整出处（2026-09-06）

> 输入：官方精读文（用户转录）+ 仓库 Tin11Mn/chrono-hybrid-mem（MIT）README。
> **身份打通**：本项目最早一条消息《赛前文章总结结果》描述的系统 = CHM（AML 学术文本榜第 5，44.33，v0.2.0/commit 7cf45c7）；P1 查询规划 0.5761、P4 归因 30=20fusion/8channel/2reranker 等全部出自此系统——后续引用『上届文章』即以本档案为源。

## 1. 与 Cortexa 的架构同源性（逐条对照）
- 六项稳定性原则 ↔ 我们：user_id 全程分区(SQL 层+缺席闸)/RAW 原文唯一来源/RARE 等注释不替代原文/确定性重排替代模型重排(我们更激进)/Search 不生成答案/复现优先=golden+check.py+retrieval-gate——**同族系统，约束设计互相印证**。
- RAW+FACT 双写+source_message_id ↔ 我们单表原文+mem_type 标签+source_request_id+supersede 链（我们省了模型抽取层，向 AMI/ReFind 靠拢）。
- FTS5 双路候选(原始+事实) ↔ 我们 fts(原文)+vec；他们通道 B=事实索引——我们无独立 fact 索引，用 mem_type/topic/entities 的弱结构替代（差距：跨表述直接性）。
- 受约束重排(ordered_ids+系统校验) ↔ 我们无重排(确定性排序)——文章方案版才有，我们 E1 已证该差异非本样本端到端杠杆。

## 2. 本地研究里程碑（可直接对我们的实验设计）
| 版本 | 组件 | 结果(1977题,本地代理) | 对我们的映射 |
|---|---|---|---|
| v0.3.0 | z-score+α0.3 融合；ColBERT 重排 Top-5 冻结 | Hit@1 .4355(词法基线 .3359, +.0996) | 重排池 Top-5 冻结优于 10/20——我们 B1 邻接/未来 rerank 实验的池大小先验 |
| v0.4.0 | 时间感知稠密键(日期元数据权重固定0.5)+Qwen3-Reranker-4B yes/no 首词概率 | .5225(+.087) | 时间元数据进键=F2 低配先行(不加解析器先验)；专用 reranker 需本地模型(沙箱外) |
| P1 | 查询规划 intent/core/expansions/entities/temporal/evidence_needs(≤4)，字段分通道，零新增 Search 调用，回退一阶段 | .5761/.6479 | **J1 候选：确定性查询规划器原型(默认关)**——我们已有 chapter 提取，缺其余字段与 evidence-need 通道 |
| P4-A | evidence need 独立有界通道×固定2槽→原重排流 | .5776(+.0015)；救回 8 题(4→Top1) | 与 H1/H5(会话/路由)及 B4 桥接同族；分桶工具我们已建(A2) |
| 负结果 | P2 集合感知重排 / P3 证据图 / P5 门控 | 全关 | 我们已同款关闭：entity/semantic/minilm/切块/权重等——负结果档案继续积累 |

## 3. 仓库最新默认（超出文章）
- 默认路径=RFF + P4-A + **Session-Fact 语义层**；P3 图默认禁；最新全量 Hit@1 **0.6108@1976**（本地代理，非官方）。
- 近期待办=冻结新规则评测协议+recall_miss 诊断集——与我们的检索质量门/分桶同构，说明『协议先行』是此族系统共同的方法论。

## 4. 动作增量（J 系列）
| # | 动作 | 来源 | 形态 |
|---|---|---|---|
| J1 | P1 式确定性查询规划器原型：core_terms/entities/temporal_cues/evidence_needs 解析→分通道+evidence-need 有界槽，零新增 Search LLM、默认关 | CHM P1+P4-A | 离线实验(可在 21 题样本+1977 级不可得时用小样本验通道增益) |
| J2 | Session-Fact 语义层评估（会话级聚合事实入口 vs 现 turn 级） | CHM repo 默认 | 与 H1 会话 RRF 合看 |
| J3 | 冻结协议+recall_miss 诊断集制度化：把 A2 分桶+retrieval-gate 升级为‘新规则先行冻结’流程文档 | CHM 近期计划 | 报名前与 METHOD_CARD(H3) 一并成稿 |

## 5. 一句话
CHM=我们最早借鉴对象的本尊：本地研究证明『查询规划(+0.24)/证据需求通道』是稳定增益、『图/覆盖/门控』是稳定负结果——与我们 A/B/E/H 系列结论完全同构；J1 把 P1 的确定性部分（core/entities/temporal/evidence-needs 分通道）移植为默认关原型，是当前检索侧最有依据的下一步。