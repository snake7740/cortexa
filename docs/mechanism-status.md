# Cortexa 机制状态总表（单一事实源）

> 版本 v1.0 | 更新 2026-09-06（批① P0 修复落地后刷新）| 维护规则：**任何机制/开关/契约改动必须同步本表**；
> 排期词汇与 README 里程碑一致（P3/P4 = 未实现阶段承诺，如实标注，禁止提前宣称）。
> 取代：Cortexa 根目录 `Cortexa_仿生AI记忆系统_完整项目文档.docx`（2026-08-29 规格，已归档为设计史参考——其包结构 consolidation.py/forgetting.py、选型 Qdrant/Neo4j/PostgreSQL、LLM gpt-4o 均非现实）。

## 一、机制总表（机制 → 代码位置 → 开关 → 状态 → 测试/证据）

| 机制/能力 | 代码位置 | 开关(config) | 状态 | 阶段 | 测试/证据 |
|---|---|---|---|---|---|
| AML 契约：Add 同步语义 + user_id 隔离 + 三 ID 回显 | models.py AddRequest/AddResponse；server.py /add | — | ✅ 已实现 | W1 | test_official_contract.py::test_add_official_payload_echoes_ids |
| AML 契约：Search data[{id,content,score?,created_at?}] + TopK=100 | models.py SearchRequest/SearchResponse/to_search_result；server.py /search | — | ✅ 已实现（2026-09-06 对照官方 API Guide 对齐） | W1 | golden=tests/golden/official_aml_contract.json；test_official_contract.py |
| Health 无鉴权 2xx + 部署口径自证 | server.py /health | — | ✅ 已实现（scene/journal_mode/api_key_required/novel_adapter_enabled） | W1/P0-1 | test_smoke/test_contract/test_official_contract |
| 30 天 TTL 数据删除（eval 启动清扫 + DELETE 兜底） | storage.record_scope/sweep_expired_eval_scopes/scope_meta 表；server lifespan | journal_mode=eval 时生效 | ✅ 已实现（2026-09-06） | P0-3 | test_official_contract.py::test_ttl_sweep_only_eval_expired |
| 端点并发安全（同步 def 入线程池） | server.py 全部端点 | — | ✅ 已实现（2026-09-06，原 async 包阻塞体修复） | P0-2 | 契约测试全绿；无 async def |
| 参赛环境一键固化 + 启动自检 WARN | Dockerfile ENV；server lifespan logger.warning | CORTEXA_SCENE/JOURNAL_MODE/NOVEL_ADAPTER_ENABLED | ✅ 已实现（2026-09-06） | P0-1 | local_eval 以 scene=aml+journal=eval 复跑绿 |
| 双通道召回（FTS5 bigram + hash 向量）+ RRF | modules/search.py；storage.fts_search/vector_search | chapter_channel=True | ✅ 已实现 | W2 | test_basic/test_contract；local_eval hit@1=0.909 |
| precision-first 截断 + 缺席闸 | search.py（空 scope O(1) 返空） | — | ✅ 已实现 | W2 | test_search_returns_empty_array |
| RULE 专属 Wing（永不衰减 + rule_boost） | models.MemoryType.RULE + no_decay；search._retention_factor | rule_boost=1.25；rule_llm_fallback=True | ✅ 已实现 | W2/维度F | test_smoke::test_rule_memory_cannot_be_forgotten |
| 维度地板审计（压缩前结构不丢） | modules/dimension_floor.py（enforce/assert_floor） | — | ✅ 已实现 | W2 | test_basic |
| FSRS 遗忘曲线检索调制 | models.FSRSState.retrievability；search._retention_factor | retrieval_fusion_weight=0.15（首过中性） | ✅ 已实现（调制）；**stability 增长未实现** | W3/P1-6 | test_review_fixes::TestFSRSRetentionModulation |
| 巩固即复习（touch） | storage.touch_retrieval | — | ✅ 已实现 | W3 | test_journal::TestFsrsRetrievalReinforcement |
| RARE-lite（检索命中两两关联） | novel_adapter.rare_strengthen | rare_enabled=True；rare_top_n=5 | ✅ 已实现 | W3 | test_rare.py |
| 完整 RARE 四步重编码 | — | — | ⛔ 未实现 | P4 | README 术语表如实标注 |
| Dreaming Cycle（重放/整合/修剪/发现） | —（docs/research/2026-08-31-dreaming-cycle-design.md 设计稿已锚） | — | ⛔ 未实现 | P3 | 设计稿；无消费代码 |
| 主动遗忘：矛盾覆盖 supersede + forget | storage.supersede/forget；domain/governance.py | governance_enabled=True | ✅ 矛盾覆盖/手动遗忘已活 | W2 | test_smoke::test_supersede_marks_old_state；governance 测试 |
| 主动遗忘：衰减遗忘引擎 + 安全遗忘条件 | —（P2-4 已收口：占位阈值已从 Settings 移除） | 无 | ⛔ 未实现（P4 落地时随消费方引入阈值） | P4 | docs/research 2026-08-31 论证 |
| 治理（维度 D）：scene-aware L1-L3 | domain/{cast,facts,lifecycle,governance}.py；schema.py（novel/aml） | scene=novel 默认（真实使用）/ aml（参赛 Docker 已固化） | ✅ novel/aml 已实现；**code schema 未实现（推断场景）** | W3+/P0-1 | test_governance_e2e（aml supersede / novel 空转对照） |
| retcon 修正链 / 实体关系图 / 编辑史 | facade.retcon/link/unlink/neighbors/entity_history；modules/graph.py | — | ✅ 已实现 | 双闭环 | test_retcon.py/test_graph.py |
| 一致性审计 / 查体五科 / 探针 | facade.audit/checkup；modules/consistency.py/checkup.py | — | ✅ 已实现（只评不改） | 双闭环 | test_consistency/test_checkup；examples/novel_* |
| 编号精确通道（第N章/第三十七章/Ch 40） | search.extract_chapter_nos + storage.chapter_search | chapter_channel=True | ✅ 已实现（小说实证 0/4→4/4） | W3 | test_review_fixes |
| 实体特征通道 | storage.entity_search | entity_channel=False（默认关） | ✅ 代码保留/默认关（A/B Δ=0） | W3 回基线 | test_review_fixes::TestFeatureCueChannel |
| 异步增强双阶段 Add | enhancer.py + pipeline daemon 线程 | async_enhance_enabled=True | ✅ 已实现（方案 A daemon；进程退出丢结果 = P1-5 待办） | W3 | test_enhancer |
| 小说适配器（实体消歧/生命周期/文风/守卫） | modules/novel_adapter.py | novel_adapter_enabled=False（参赛关）；adapter_* 分开关 | ✅ 已实现 | 双闭环 | test_cast/test_lifecycle_engine/test_relations；小说 208 章实测 |
| lifecycle guard 警告进 content | novel_adapter.guard_lifecycle | adapter_lifecycle_guard=True | ⚠️ 已实现但 P2-5 未修（污染证据原文纯度；参赛默认关故不触发） | P2-5 | — |
| LLM 韧性（超时/重试/熔断/降级） | llm/resilience.py + /health llm_resilience | llm_retry_max 等 | ✅ 已实现 | W2 | test_llm_resilience |
| 任务流水 journal（app/eval/off 脱敏） | journal.py；pipeline/search 写入 | journal_mode | ✅ 已实现 | W2 | test_journal |
| scope 元数据/TTL 登记 | storage.record_scope（run_add 内自动） | source=eval if journal_mode=eval | ✅ 已实现（2026-09-06） | P0-3 | test_official_contract::test_ttl |
| 持久化/检索性能 | SQLite 三合一 + 向量缓存分页 | — | ✅ 现状；已知债务：P1-1 search 同步写、P1-2 治理 N+1、P1-3 embed 300ms>200ms（openai 路径）、P1-4 except×9 | P1 | — |
| 链接双写（links_json vs links 表）检索零消费 | storage.add_links/memories.links_json vs graph.links | — | ⚠️ 债务（P2-1：收敛 + 邻居提升 A/B 未做） | P2-1 | — |
| 质量闸 | pytest（tests/ 24 文件） | — | ✅ 239 passed（2026-09-06 实跑 29.4s）；**无 git/CI/lint**（P1-7 open） | P1-7 | 本表即审计证据 |

## 二、排期衔接（README 里程碑对照）

- 报名周 9/20-9/26 前必做：已全部落地（P0-1..P0-4）；剩 P1 批次按 review-issues-plan 顺序执行（可靠/性能/卫生）。
- P3-1 前置：P1-6（stability 最小增长）→ Dreaming 开工；P2-1 A/B → 多跳机制开工裁判。
- 完整 RARE/衰减遗忘/NER/遗忘引擎接线：P4 承诺，落地前 README/本表保持「未实现」如实标注。

## 三、术语一致性提醒（DDD）

- 官方契约词：user_id（检索隔离）↔ 内部 scope（同义，HTTP 层收口）；data/content/created_at（Search 响应）↔ 旧 results/memory/timestamp（仅内部属性兼容）；
- session_id 仅分组不做隔离；RULE=规则类永不衰减；retcon=修正链；TTL=30 天合规清扫。