# Cortexa 自审问题台账与解决计划

> 生成日期：基于 2026-08-31 ~ 09-04 代码基线（workspace C:\Cortexa）的深度自审
> 方法：逐模块精读 + 231 项测试实跑 + 并行考古（8 份设计/复盘文档、22 个测试文件、17 个示例脚本）交叉核验
> 纪律：每条问题带文件行号证据；宁少报不可误报；只列真问题，不重复 README 已诚实标注且无后果的备注
> 优先级：P0=合规/契约/进程安全（先修）→ P1=延迟红线/可靠性/可观测 → P2=设计债务 → P3=功能排期衔接

## 处置记录（2026-09-06 · 批① 报名前合规批落地）

| 条目 | 处置 | 证据 |
|---|---|---|
| P0-1 场景默认值/health 暴露 | ✅ 已修：/health 暴露 scene/journal_mode/api_key_required/novel_adapter_enabled；lifespan 启动自检 WARN（journal≠eval / scene≠aml 打醒目日志）；Dockerfile 固化 ENV（SCENE=aml/JOURNAL_MODE=eval/ADAPTER=false）；.env.example 增「参赛必设三键」+ CORTEXA_SCENE；README 参赛部署节 | server.py / Dockerfile / .env.example / README；test_official_contract::test_health_any_2xx_no_auth |
| P0-2 async 包阻塞体 | ✅ 已修：4 端点改同步 def（FastAPI 入线程池；SQLite conn 本就 check_same_thread=False + RLock） | server.py docstring + 端点定义；契约测试全绿（239 passed） |
| P0-3 30 天删除无 TTL | ✅ 已修：scope_meta 表 + record_scope（run_add 自动登记，eval source）+ sweep_expired_eval_scopes（eval 部署启动清扫）+ DELETE 同步清 scope_meta | storage.py；test_official_contract::test_ttl_sweep_only_eval_expired |
| P0-4 契约占位 | ✅ 已修（升级为真实对齐）：对照仓库根 _ref_apiguide.html（官方 API Guide）迁移契约——Add=request_id/messages/user_id/session_id，Add200=success+三ID 逐字节回显；Search=query/options?/user_id/top_k(正式100)；Search200=data[{id,content,score?,created_at?}]；golden 固化 | models.py 契约段 / server.py / tests/golden/official_aml_contract.json / tests/test_official_contract.py（8 项）；local_eval 以官方键复跑分数不变 |
| P1-5b Enhancer schema 场景漂移 | ⏳ 顺带登记未动（随 P1-5 一并排期） | — |

**P2-4 ✅（2026-09-06）**：未接线阈值已从 Settings 移除（无消费方，P4 落地时随消费方引入）。

**P1-6 ✅（2026-09-06）**：最小 stability 增长落地（S'=S×1.15，封顶 3650d；测试 3 项）。

**仍 open（按原台账执行）**：P1-1/2/3/4/5a/7；P2-1/2/5/6/7；P3-1。详细现状见 docs/mechanism-status.md（单一事实源）。

---

## P0 — 合规 / 契约 / 运行安全

### P0-1 服务端场景默认值陷阱：参赛部署下「维度 D 治理」与「日志脱敏」默认失效且无提示
- 证据：cortexa/config.py:99 scene 默认 novel、config.py:108 journal_mode 默认 app；cortexa/server.py:55-63 create_app 直接消费默认值；而 cortexa/domain/governance.py:30 用 novel schema 的事件谓词（全是中文死亡/离开词）治理 AML 英文偏好——examples/local_eval.py:10-12 注释实证「治理词全部空转（2026-09-04 实测漂移）」，其修复方式是只在 eval 脚本内硬编码 scene=aml，服务端路径并无此处理。
- 影响：按 README/Docker 起服务参赛时，维度 D（行业最大短板 51.2 分）静默失效；journal_mode=app 会把 query/消息原文落库，与官方「评测数据不留日志」红线冲突；.env 实测未设这两个键。
- 动作：
  1. /health 与启动日志暴露 scene / journal_mode / api_key_required / novel_adapter_enabled；
  2. Dockerfile/部署文档固化参赛环境（CORTEXA_SCENE=aml、CORTEXA_JOURNAL_MODE=eval、adapters 关闭）；
  3. create_app 启动自检：若 scene=novel 且 journal_mode=app 则打醒目 WARN（不静默）；
  4. .env.example 增补 CORTEXA_SCENE 说明与「参赛必设」清单。
- 验收：以默认 env 起 uvicorn，/health 直接可见治理场景与日志模式；README 部署节给出参赛一键环境。
- 规模：S（config/server/README，约 1h）

### P0-2 HTTP 端点用 async def 包阻塞同步体：单进程内事件循环被 SQLite/httpx/重试 sleep 卡死
- 证据：cortexa/server.py:97,104,121,146 四个端点均为 async def，函数体直接同步调用 run_add/search_memory（内部 sqlite 读写、openai httpx 同步调用、resilience 的 time.sleep 退避）。FastAPI 对 async def 不进线程池，全部请求串行排队。
- 影响：并发评测流量下 P95 急剧劣化，直接威胁 Search 200ms/Add 500ms 红线；熔断退避 sleep 会冻结整个服务。
- 动作：端点改 def（FastAPI 自动入线程池，配上限）或用 anyio.to_thread 包阻塞段；保留中间件；补并发冒烟（10 并发 /search）验证。
- 验收：并发压测下吞吐不随并发塌缩；既有契约测试全绿。
- 规模：S

### P0-3 官方「评测数据 30 天内删除」只靠手动 DELETE，无自动过期兜底
- 证据：cortexa/storage.py:667 clear_scope 是唯一清理入口，由 server.py:145 手动触发；库中无 scope 元数据表，无从计算入库年龄。
- 影响：操作者忘记调用即合规违约（无版本控制 + 无 TTL，违约不可追溯）。
- 动作：新增 scope_meta 表（scope/首末写入时间/来源标记），写入时惰性登记；eval 部署下启动清扫或后台任务删除超过 30 天的 eval scope（真实使用 scope 不标记不删）；DELETE 端点继续保留。
- 验收：种入带旧时间戳的 scope 后自动被清扫；真实 scope 不受影响；有单测。
- 规模：M（storage/schema/单测）

### P0-4 AML 契约字段尚有三处「待样例确认」占位
- 证据：cortexa/models.py:12,155,202（AddResponse.status=ok 的取值、字段别名为猜测，须等官方 Smoke 回执）。
- 影响：Smoke 若返回结构不符，全链路契约崩（集中在 AddResponse/SearchResult 别名层，改动面小）。
- 动作：建立 tests/test_official_contract.py 参数化桩，把「假设的官方样例」固化成 golden 文件；9/20 Smoke 回执后只改 golden 与别名表；把 refs/aml-contracts 六个管线中可确证的契约断言抽入测试。
- 验收：一份 golden 文件即可全量切换字段语义。
- 规模：M

---

## P1 — 延迟红线 / 可靠性 / 可观测性

### P1-1 Search 热路径同步执行写操作（命中强化 + RARE-lite 关联写回）
- 证据：cortexa/modules/search.py:158-168：results 排序后、返回前同步执行 store.touch_retrieval（N 条 UPDATE+1 commit）与 NovelAdapter.rare_strengthen（top_n=5 次 add_links，每次独立 commit，storage.py:640-665）。
- 影响：检索时延随命中数线性增加（top_k=90 冒烟时多次提交）；Search 变有状态——重复/并行查询会改排序（首次评测无复习零退化，但二次 Full 或平台重放会漂移）；写失败被 except:pass 吞掉无痕。
- 动作：三选一并记录决策：(a) 移至返回后异步/攒批；(b) 至少单事务批处理 + deadline 预算内执行、超时跳过；(c) 若保留同步，/health 增加 search_write_ms 观测并明确「检索有状态」语义。附带：RARE 写回与 journal 记录失败需进观察面（见 P1-4）。
- 验收：压测 Search P95 不随命中数增长；文档明示状态性。
- 规模：M

### P1-2 写入期治理为 N+1 全扫，且 subject 无列无索引
- 证据：cortexa/domain/governance.py:56-72 find_conflicts 对每条新记忆先 SELECT 全部 ACTIVE id、再逐条 store.get（每条一次独立查询），subject/slot 在 Python 层从 content 推导，无物化列。
- 影响：逐 chunk Add 场景（平台按消息发送）下每请求 O(scope 规模) 查询，总量 O(n^2)；scope 上万条后 Add 预算与治理开销失控。
- 动作：物化 subject/slot 列 + 索引；find_conflicts 单条 JOIN 查询 + 上限扫描；治理失败计数可见（不静默，见 P1-4）。
- 验收：万条 scope 下逐 chunk Add 仍低于预算；governance 单测补大库用例。
- 规模：M

### P1-3 检索嵌入预算(300ms)与 Search 红线(200ms)冲突，且未传导 deadline
- 证据：cortexa/config.py:48 llm_timeout_embed_ms=300 而 config.py:84 search_p95_ms=200；cortexa/modules/search.py:132-140 调 llm.embed 时未传 deadline_ms，embed 可独占近 300ms——仅此一项即击穿红线（hash 提供者离线才无恙）。
- 影响：若参赛侧启用 openai 语义嵌入，Search 红线结构性超限。
- 动作：openai embed 路径传 deadline_ms=search 预算剩余；查询向量加 LRU 缓存（重复题命中零成本）；红线自证表按 provider 区分观测列。
- 验收：openai provider 下 /health search_p95<200（本地软断言，不引入 CI flake）。
- 规模：S-M

### P1-4 全库 9 处 except Exception: pass：增强/治理/建边/写回失败零可见
- 证据：grep 统计 pipeline.py x4、search.py x1、enhancer.py x1、governance.py x1、checkup.py x1（resilience.py 1 处属结果化转换，不算）；LLM 韧性层有 ResilienceStats 且 /health 暴露，但这些非 LLM 增强面失败完全没有计数器。
- 影响：某次治理静默失效/异步增强全部失败/关系建边全拒时，系统照常返回 200，事后无法定位——与项目「声明即执法」文化相悖。
- 动作：仿 ResilienceStats 加 EnhanceStats（govern/rare/enhance/graph_fail 计数 + 最后错误），/health 与 journal warn 事件暴露；pipeline/search 内部 catch 只吞「可预期增强失败」，编程错误（TypeError 等）不再吞。
- 验收：人为注入异常后 /health 计数增长、日志有记录。
- 规模：M

### P1-5 异步增强两个次生问题：进程退出丢结果 + scene 不一致
- 证据：cortexa/modules/pipeline.py:148-173 daemon 线程在进程退出时可能未完成即被杀（scene-aware 文档已承认方案 A 局限）；cortexa/enhancer.py:44-46 Enhancer 默认 get_schema(novel)，不读 settings.scene——AML 场景下仍拿小说 schema 兜底（当前因注入 entities 而无害，属潜在漂移）。
- 动作：增强任务入队改为可等待的 executor（服务退出前 drain 或明确丢弃并计数）；Enhancer schema 改从 settings.scene 解析；增强结果计数进 P1-4 的 EnhanceStats。
- 验收：模拟退出场景计数不丢；scene=aml 时增强用 aml schema。
- 规模：S-M

### P1-6 「巩固调度器」名实差距：FSRS 只有遗忘曲线，没有强度增长
- 证据：README/models.py 宣称「FSRS 算法管理记忆强度，用得越多越牢」；但 cortexa/storage.py:616-638 touch_retrieval 只重置 last_review_at、consolidation_level+1，从不更新 stability/difficulty——重复命中不会让 S 增长，R(t) 仅靠时间重置复原。
- 影响：核心卖点机制目前只兑现了「检索即复习」一半；若评测维度 C/D 深挖遗忘曲线，现有行为与声明不符。
- 动作：诚实二选一——(a) P3 实现最小 stability 更新（S 随成功检索增长，按成功/遗忘事件驱动）并配单测；(b) 在未实现前把 README/注释措辞改为「遗忘曲线检索调制（FSRS 模型占位，强度更新 P3）」，杜绝名不副实。
- 验收：连续命中的记忆 R(t) 衰减显著放缓；措辞与实现一致。
- 规模：M（机制）/ S（措辞）

### P1-7 无版本控制 / 无 CI / 无 lint：全部质量闸靠手工
- 证据：git status 非仓库；glob 无 .github/ruff/pre-commit；唯一跑测入口是带沙箱补丁的 _run_pytest.py；_replace_method.py 是裸执行的行号改写脚本（曾原地改 novel_adapter.infer_relations），存在误运行风险。
- 动作：git init + 基线提交 + .gitignore（.pytest_work/data 下 db/.env/novel 数据）；ruff+typing 配置入 pyproject；scripts/check.py 汇总 lint+type+test（复用 _run_pytest 沙箱补丁）；CI 留作可选项。
- 验收：新机器 clone 后一条命令全绿。
- 规模：M

---

## P2 — 设计债务（已写未消费 / 双份 / 表述漂移）

### P2-1 双份链接存储且检索零消费：links_json（memories 表）与 links（graph 表）
- 证据：RARE 写 memories.links_json（storage.add_links），enhancer/novel_links 写 graph links 表；检索融合（search.py）两条都不读；facade.entity_history 只读 graph 表。wing/room 同理：写入了但除 RULE 外检索从不按 room 过滤。
- 影响：维度 B 的「簇间桥梁/拓扑」目前只是记账，未转化为召回/排序收益；两份数据语义边界模糊，未来维护易错。
- 动作：决策后收敛——最小可行：graph.links 为唯一权威，rare 写回也落 graph（带 evidence=query）；在融合层加邻居提升（候选记忆的实体若与查询实体有活跃边则 RRF 加分），走 local_eval A/B 验证，伤精度即撤；room 暂标注「预留，检索不消费」。
- 验收：A/B 报告留档；无死代码双写。
- 规模：M-L

### P2-2 术语表 RARE(P4) 与已实现 RARE-lite 并存易误读
- 证据：README 术语表「RARE 检索后四步重编码写回 P4」；代码注释（novel_adapter.py:343）称「RARE-lite…文档 RARE 四步的关联强化子集」且默认开。
- 动作：README/文档统一为「RARE-lite（已上线，检索命中两两关联）/ 完整 RARE（P4）」两行；代码 docstring 同步。
- 规模：S（纯文档）

### P2-3 顶层规格 docx（2026-08-29）与代码现实漂移
- 证据：docx 第 12.4 节包结构含不存在的 consolidation.py/forgetting.py、技术选型 Qdrant/Neo4j/PostgreSQL、LLM gpt-4o——现实是 SQLite 三合一 + 仅 gpt-4o-mini。
- 动作：归档旧 docx 并标注「设计史参考」；在 docs/ 建立单一事实源《机制状态总表》（机制→文件→开关→实现状态→排期），README 术语表指向它，杜绝二义。
- 规模：S（归档）+ M（总表）

### P2-4 config 里声明的未接线阈值应「接线或删除」（项目自身纪律：禁止只声明）
- 证据：config.py:85 evidence_token_cap、86 write_token_budget、90 consolidation_core_level、95-96 forget_cosine_max/forget_llm_relevance_max 均标注未接线。
- 动作：写消费方或删除；保留项必须有真实消费路径与测试（如 evidence 截断在返回前；forget 阈值在 P4 遗忘引擎落地时引入，落地前从 Settings 移除避免 API 误用）。
- 规模：S（删）/ M（接线）

### P2-5 guard_lifecycle 向 SearchResult.memory 原文追加警告标记，违背「返回证据原文」纯度
- 证据：novel_adapter.py:426-458：警告文本直接拼进 memory 字段（AML 平台答案模型会读到非原文内容；当前因 adapter 默认关而未触发）。
- 动作：标记移入独立结构（扩展字段或仅日志/独立提示面），memory 保持原文；或在文档明确「真实使用场景允许，参赛默认关」的边界。
- 规模：S

---

## P2/P3 — 工程卫生与测试补强

### P2-6 测试缺口清单（测试考古结论）
- run_add 无直接单测（仅经 facade/server 间接）；config env 优先级无矩阵测试；storage/models 无专属测试文件；encoding/rule_wing/dimension_floor 挤在 test_basic；llm/client 网络路径零直测（刻意离线，保持）；无延迟/规模测试（200ms/500ms 红线无 load 断言）。
- 动作：补 tests/test_pipeline.py（run_add 直测：空消息/去重/治理触发/增强开关矩阵）、tests/test_config_matrix.py（env/文件/构造参数优先级表）、examples/stress_smoke.py（种 2000 条→连发 /search 观测 p95，供平台前检，不进 CI 免 flake）。
- 规模：M

### P2-7 工作区/仓库卫生（不动真实数据，先归档后删，需你确认）
- 高危留档：_replace_method.py（裸改写源码，归档+警示或删除）；_tmp_*.py x7、_a_probe/_backfill_test/_p1_test（考古性 scratch）；style_report_v2/v3.json（代码已无写出路径的旧 schema 留档）；.novel_probe_root/（29MB 零引用残留）；.backups/*.db（29MB 备份，移出项目目录）。
- 动作：统一移入 cortexa-mvp/_archive/ 或删除（逐项经你确认，尤其含 .env 的备份与 DB）。
- 规模：S

### P3-1 功能缺口衔接（与 README 既有排期一致，不新增承诺）
- Dreaming Cycle（P3：需先定 KL 冗余判据再动手，设计稿已锚定）；完整 RARE 四步（P4）；衰减遗忘引擎 + 安全遗忘阈值接线（P4，接线前先删 P2-4 的占位）；中文专名 NER（P4，实体通道/多跳的地基）；巩固稳定性更新（见 P1-6a）。
- 前置关系：P1-6 → P3-1；P2-1 A/B → 多跳机制是否开工的裁判；P0-4 → 一切契约面改动的地基。

---

## 验证过「非问题」项（避免误报，自审对称）
- .env.example 为合法 UTF-8（早前 PowerShell 控制台显示乱码为解码假象，已用字节级验证）；
- conftest 隔离（剥 env + chdir）足够且必要；231 项测试全绿（沙箱安全跑测器 _run_pytest.py）；
- search 只返回证据、scope 精确隔离、RULE 永不可遗忘等红线均有实现与测试锁；
- 分页向量缓存已消除 O(N^2)；hash 嵌入与 CJK bigram FTS 双侧对称；
- config/README 对「未接线」标注整体诚实（除 P2-4 建议的收口）。

## 建议执行顺序（依赖优先）
1. 第一批（P0，半天）：P0-1 场景默认值 + /health 暴露 → P0-2 端点线程池 → P0-4 golden 契约桩
2. 第二批（P1 可靠性，1 天）：P1-4 增强观察面（其它 P1-1/2/3 的可见性前提）→ P1-7 git 基线 → P1-6 措辞诚实化
3. 第三批（P1 性能，1 天）：P1-3 embed deadline → P1-2 治理物化 → P1-1 search 写路径决策
4. 第四批（P2+卫生，1-2 天，含你确认项）：P2-1 链接收敛+A/B、P2-3 状态总表、P2-6 补测、P2-7 清理