# METHOD CARD — Cortexa（第二届 Agent Memory Challenge · 开源方法榜）

> 提交材料之一。对应官方要求：公开仓库 + 固定 commit + 方法说明 + 思想来源披露。
> 本卡只写**评测版本实际生效**的东西；默认关闭的机制一律不写进"方法"，只在 §6 列为"未启用"。
> 仓库与代码 commit 已填（已推公开仓）；公网端点已部署并通过契约自测。

## 1. 基本信息

| 项 | 值 |
|---|---|
| 系统名 | Cortexa — 零模型确定性记忆检索系统 |
| 版本 | 0.1.0（`cortexa/__init__.py`） |
| 公开仓库 | https://github.com/snake7740/cortexa （公开，MIT） |
| 评测 commit | `0caf86993a722ad258325f668ba63c4401286d38`（**代码冻结点**，公网端点镜像由它构建；官方受理后不变）。它是 `fb56f83` 的后继——官方规则是「受理后才冻结」，受理前又发现一个并发正确性 P0（`TaskJournal` 与 store 共用同一个 sqlite3 连接但各持一把独立 RLock），必须修：不修则并发灌库时返 500 并**静默丢记忆**，比任何护栏都致命；同时把 deadline 阈值从 200ms 重定到 5000ms（200ms 对齐的是我方内部红线，不是官方约束，详 §8）。上一批的运行时护栏（准入阀/deadline/返回预算）仍在本 commit 内，**不改 Add/Search 地址、不改返回内容与字段**；改动说明写在 commit `0caf869` 正文里（版本登记要求）。其后的提交仅修订 README 与本卡表述，**不改任何代码、配置、测试与镜像内容**——可执行 `git diff 0caf869 HEAD -- cortexa tests scripts examples Dockerfile requirements.txt pyproject.toml .env.example` 复核，结果应为空。 |
| 公网端点 | `http://47.79.226.71:8000`（Add `/add`、Search `/search`、Health `/health`）；同容器另绑标准端口 `http://47.79.226.71`（80）作为后路，两端点同一实例同一数据卷。由评测 commit 构建的镜像单容器常驻，容器 `--restart unless-stopped`。契约自测（`scripts/smoke_local.py` 对该公网地址）全部通过：Add 200 / Search 命中 / 无 key 与错 key 均 401。 |
| 官方登记版本 | 榜单侧版本名 `0.1.0-aml-0caf869`（`version_e2f31aea1ff0`，2026-10-30 自助改绑，与评测 commit 对齐；鉴权沿用 Bearer + Memory System Key）。学术榜。端点登记一次覆盖三赛道，故官方 `track-eligibility` 回显 textual / coding / multimodal 三项均为 `bound=true`；但**本期我方拟参评类型仅为文本记忆**（见 §9「赛道面」）。详见 §9。 |
| 鉴权 | Memory System Key；`Authorization: Token <k>` / `Bearer <k>` / `X-Api-Key: <k>` 三头任一 |
| 语言/运行时 | Python ≥3.11，FastAPI + SQLite（FTS5），单进程单容器 |
| License | MIT |

## 2. 模型使用声明（开源方法榜硬规对照）

| 组件 | 官方要求 | 本系统评测版本 |
|---|---|---|
| Embedding | 仅允许 text-embedding-v4 | **不使用任何嵌入模型**（`CORTEXA_EMBEDDING_PROVIDER=hash`：确定性特征哈希，非学习型，零外部调用）。已就 v4 语义通道按官方口径 250 题成对 A/B：各项指标均劣于 hash 基线，因此**基于证据主动关闭**（非尚未实验）。代码保留 openai provider 能力，若开启则显式下发 `dimensions=512` |
| LLM 组件 | 仅允许 gpt-4o-mini | 评测版本 **Add/Search 路径不调用 LLM**（`CORTEXA_ASYNC_ENHANCE_ENABLED=false`、`CORTEXA_RULE_LLM_FALLBACK=false`、领域适配器关闭；规则引擎未命中直接落 FACT，不兜底调模型）。代码中 `llm_model` 默认值固定为 `gpt-4o-mini`，若开启则不会使用其他模型。**可申报 N/A（全链路零生成模型，官方 Q7）** |
| Reranker | 不限 | 无独立 reranker 模型；融合后的重排为规则函数（见 §3.3） |

> 结论：评测版本是**无模型调用的纯检索系统**。所有分数来自词法检索 + 规则融合 + 记忆状态调制。

## 3. 方法概述

### 3.1 Add（写入）
1. 官方 `messages[]` 按消息粒度切为记忆实例，保留 `role`、消息级 `timestamp`（Unix ms→ISO8601Z），`user_id` 映射为内部 `scope`（精确隔离，不做前缀/模糊匹配），`session_id` 作为会话锚。
2. **规则 Wing 编码**（`modules/rule_wing.py` + `modules/encoding.py`）：对内容打记忆类型标签（事实/事件/偏好/任务…）与**七维度地板**特征（`modules/dimension_floor.py`：显式事实 A / 多跳 B / 时间事件 C / 治理 D / 个性化 E / 上下文学习 G / 认识论安全 H），纯规则，可复现。
3. 同步写入 SQLite：正文入 FTS5 索引；向量列按 `embedding_dim=512` 建矩阵（hash 模式下为确定性哈希向量）。
4. **幂等**：重复写入不产生重复记忆，重放仍返回 200/`success:true`，`request_id` 逐字节原样回显。机制如实：唯一键是数据库的 `UNIQUE (scope, content_hash)` + 忽略式插入（`storage.py`），**不是以 `request_id` 建唯一约束**；`request_id` 另落 `source_request_id` 供追溯。因官方重试语义是「保持同一 request_id 与请求体」，重试必携带完全相同内容，故在重试路径上内容级去重与 ID 级去重结果等价。连带特性：同一 `user_id` 内原文完全相同的两条内容只保留一条。
5. Add 返回前全部落盘，返回后立即可被 Search（同步写入契约）。

### 3.2 Search（检索）
`modules/search.py`：多通道召回 → RRF 融合 → 规则加成 → 保留度调制 → precision-first 截断。
- **通道**：FTS5 词法（主通道，维度 A）；向量通道（hash 模式下为词面哈希相似，辅助）；编号精确通道（章节/编号强意图，评测中基本不触发）。
- **融合**：Reciprocal Rank Fusion，`score = Σ w_c / (rrf_k + rank_c)`，词法权重 / 向量权重可配（默认按 2026-09-06 E1 成对实验定值）。
- **查询意图**（`modules/query_intent.py` / `query_plan.py`）：规则识别时间指向、否定、列表意图，调整通道宽度与过滤。
- **同会话邻接扩展**（`modules/adjacency.py`，`CORTEXA_ADJACENCY_ENABLED=true`）：融合序前 20 个种子各补同会话 ±1 时间邻居，按"前邻→种子→后邻"插位、去重、严格 ≤ top_k 预算。机制取自榜首公开方案（InvMem；AMI 亦有同类实现，见 §7），纯检索期重排、零模型调用。
- **时间件**（`modules/temporal_filter.py` / `time_anchor.py` / `recency_arbitration.py`）：基于消息级时间戳的近因仲裁与时间窗过滤——评测版本**默认关闭**（见 §6）。
- **截断**：严格 ≤ `top_k`（正式固定 100），只返 `id/content/score/created_at`，不返回答案文本（回答由平台生成）。

### 3.3 记忆状态调制
> 定位说明：本节机制早期被当作核心卖点（仿生记忆），实测后已降级为**辅助与真实使用侧**：评测档无复习史，保留度调制属中性，不构成本系统的提分宣称；本系统分数来自 §3.2 的检索路径。
- **保留度**（`modules/decay.py`）：借鉴 FSRS 间隔重复的 DSR 模型（难度/稳定性/可提取性）给每条记忆一个保留度因子，参与最终排序的乘性调制；评测档关闭 touch（检索不改状态），保证 Search 是 `f(user_id, 已存记忆, 当前 query)` 的纯函数，跨样本无状态泄漏。
- **治理 / 遗忘**（`modules/content_forget.py` / `consistency.py`，`CORTEXA_SCENE=aml` schema）：显式删除/撤回类消息触发对应记忆的治理标记，Search 侧按标记降权或排除（维度 D）。

### 3.4 合规实现
- `CORTEXA_JOURNAL_MODE=eval`：任务流水只记统计，不落 query/消息原文（"不记录不必要请求正文"）。
- `/health` 在 eval 档只返 `{"status":"ok"}`（官方 Q21：无鉴权端点不暴露密钥/模型配置/库地址）。
- 30 天删除双保险：启动时 TTL 清扫超 30 天的 eval scope + `DELETE /scope/{user_id}` 人工兜底。
- 禁硬编码公开题；评测路径无任何数据集特判。

## 4. 复现

```bash
git clone https://github.com/snake7740/cortexa.git && cd cortexa && git checkout 0caf86993a722ad258325f668ba63c4401286d38
docker build -t cortexa . && docker run -d -p 8000:8000 -e CORTEXA_API_KEY=<k> -v cortexa-data:/app/data cortexa
python scripts/smoke_local.py http://127.0.0.1:8000 --key <k>     # 契约自测
python scripts/check.py                                           # compileall + ruff + retrieval-gate + pytest
```

Docker 镜像固化 `CORTEXA_SCENE=aml / JOURNAL_MODE=eval / NOVEL_ADAPTER_ENABLED=false / ASYNC_ENHANCE_ENABLED=false / RULE_LLM_FALLBACK=false / ADJACENCY_ENABLED=true`；外部仅需注入 `CORTEXA_API_KEY`（及可选的 v4 嵌入三件套）。

## 5. 本地质量门（非官方成绩，不可换算）

`scripts/retrieval_gate.py` 对 `tests/golden/retrieval_baseline.json`：hit@1 0.909 / hit@5 1.000 / MRR 0.939 / para_direct 0.544（2026-10-30 在评测 commit 上复跑 PASS，与基线零回退）；pytest 337 用例绿（含 `TestSharedConnectionLock` 并发装配执法）。邻接扩展上游数据成对回放（250 题，本地代理口径）：命中 94→129，逐题 0 回退。
这些是**回归门**，用于防退化，不代表 AML 任何赛道得分。

## 6. 评测版本未启用（仓库中存在但默认关）

| 机制 | 模块 | 状态 | 原因 |
|---|---|---|---|
| 语义嵌入通道 | `llm/client.embed` + `CORTEXA_SEMANTIC_CHANNEL` | 关 | **已做官方口径 250 题成对 A/B，v4 各项均劣于 hash 基线**，基于证据关闭 |
| 异步双阶段增强（实体/关系补全） | `enhancer.py` | 关 | 最小模型面；预算确定性 |
| 领域适配器 / RARE-lite 检索重编码 | `modules/novel_adapter.py` | 关 | 面向小说场景；评测要求 Search 纯读 |
| 实体通道 / 会话 RRF / 时间过滤 / 近因仲裁 | `modules/graph.py` `session_rrf.py` `temporal_filter.py` `recency_arbitration.py` | 关 | 未通过成对实验 + retrieval-gate，不进默认 |
| touch（检索改状态） | `decay.py` | 关 | 保证 Search 纯函数、跨样本无状态 |

## 7. 思想来源披露

- FSRS（Jarrett Ye）—— 保留度 DSR 模型与遗忘曲线公式
- 测试效应（Karpicke & Roediger, 2008）—— RARE 检索增强重编码的依据（评测版未启用）
- MemPalace / A-MEM / LightMem —— 拓扑地图与离线整理的工程借鉴（评测版未启用）
- Reciprocal Rank Fusion（Cormack et al., 2009）—— 多通道融合
- **InvMem / ActiveMemoryIndex（AML 首期开源榜第 1/3 名，官方公众号公开机制说明）** —— 同会话邻接扩展（前 20 种子 ±1 邻居）为本系统直接借鉴并独立实现；选择题 options 拼接查询亦借鉴 InvMem 查询构造
- AML 官方契约仓库 `AML-memory/agent-memory-leaderboard` 公开文件 —— 协议与评分行为参考
- 数学约束类比（维度地板/安全遗忘条件）为本项目的类比迁移，非严格定理

## 8. 已知局限（如实）

- 评测版本无学习型嵌入，多跳（B）与同义改写召回弱于 dense 方案；真实分布本地探针召回约 1/88。
- 时间件默认关，维度 C 依赖词法命中时间表达。
- **运行限制（如实）**：单实例单进程。契约面（`/add`、`/search`）有在途准入阀：超软上限
  （默认 64）在事件循环内排队最多 2000ms，等到即照常返 200；只有在途 ≥ 硬上限
  （默认 512）才返 429 + `Retry-After`——形状是**排队为主、429 只当崩溃阀**（官方
  /docs 06：429 非强制实现项，超限表现为排队后返 200 则不进任何错误码）；未鉴权
  请求走 401，不占配额。Search 另有请求内 deadline（默认 **5000ms**）与返回预算
  （默认 60000 token 估算，同官方 Q18「低优先级先剪」从尾部自剪）；deadline 到点只跳
  「邻接扩展 / 选项副通道」这类加料步、不动召回源，仍返 200 降序数组。
- **deadline 阈值依据（2026-10-30 修订，原值 200ms 作废）**：官方单请求时限最长 30
  分钟、计分维度不含延迟（`competition-rule-freeze-v1.md` 第 25/41 行），200ms 对齐
  的是我方内部业务红线 `search_p95_ms`，不保护任何比赛约束。实测（本机，与 Docker
  同档位，deadline 显式关闭）6000 条语料 Search p50 41.0 / p95 83.9 / max 110.9ms，
  邻接扩展成本 ≈ +9ms；按 200ms 会在最大语料（即最难的题）上静默关掉本地官方口径
  +35 命中的邻接扩展。改 5000ms（≈45× 实测最差值）后只做真卡死兜底，正常负载零行为
  变化。阈值均可以 `CORTEXA_*` 环境变量覆盖；app 档 `/health` 的 `admission` 与
  `enhance_stats` 计数为运行时自证（eval 档按 Q21 不开信息面）。
- **并发正确性（2026-10-30 修复）**：`TaskJournal` 复用 `SQLiteMemoryStore` 的
  `sqlite3.Connection`，原先两类各持一把互不相干的 `RLock`——两把锁保护同一个非线程
  安全对象等于无保护。公网 `c=16` 灌库实测 2/48 个 500（`c=1/4/8` 各 48 请求全 200）；
  本机 16 线程 × 300 次混合作业对照实证：独立锁 23 次异常并**静默丢 1 条记忆与 10 条
  流水**，锁同源 0 异常、300/300 全落库。现由 `server.py`/`facade.py` 注入 `store.lock`，
  `tests/test_journal.py::TestSharedConnectionLock` 同时钉死装配同源与并发零异常。
- 本期（10/31 前）只有 1 次 Full 机会，无官方诊断回路，优先级定在"契约零错 + 稳定在线"而非分数冲刺。

## 9. 官方评测状态（平台回显原文，非本地推算）

| 项 | 值 |
|---|---|
| 版本改绑 | `version_e2f31aea1ff0` = `0.1.0-aml-0caf869`，`status=active`，`memory_api_key_bound=true`，`api_auth_scheme=bearer`。官方 2026-10-30 邮件回显的 Add/Search Token 与容器实际运行 key 逐字符相同（64 hex），首请求 401 风险已排除。 |
| textual Smoke | `teval_b0ce14629963d965` **succeeded**（2026-10-30T04:10:08Z，46/46 题，零 failures）。总分 **0.6377**。 |
| 能力维 | A 0.800(5) / B 0.667(3) / C 0.900(4) / D 0.394(6) / E 1.000(2) / G 0.550(4) / H 0.583(2) / streaming 0.448(28) |
| 短板子能力 | F1 0.167 / G2 0.200 / D1 0.333 / D2 0.400 / streaming.online 0.440(26) |
| textual Full | **已受理并冻结**：`teval_9a721518121971b3`，label `full-0caf869-textual`，2026-10-30T05:12:27Z 起跑，参数与已通过 Smoke 完全一致（full / Add 64 / Search 128 / top_k 100）。提交前契约自测全绿（Bearer 口径：Add 200 1238ms、同 request_id 重放 200、写后立读命中、错 key/无 key 401、DELETE 200）。Add 真实总量 **42176 条 = textual 分支 38183 + streaming 分支 3993**（首帧只报 streaming 分支的 3993，不是总量）。受理后代码冻结，不可回退。 |
| 官方受理通知 | 官网系统邮件（2026-10-30）确认开跑，硬口径：完整评测通常 0.5—2 天（实际可更长）；单 Add/Search 请求最长等待 30 分钟；本期允许 Add 16—64、Search 16—256；平台对部分临时异常**有界重试**；须按 request_id 幂等、按 user_id 隔离、**运行中不得清空记忆或提前回收**（部分赛道交替执行 Add 与 Search）。逐条对账结论：①本系统零外部模型、零上游 API，「欠费/额度/密钥过期」类中断风险天然免疫；②准入阀实测为「超软上限 64 先排最多 2000ms、排完照常处理，仅在途 ≥512 才 429」（`server.py` `_AdmissionGate.acquire`）——64+128=192 峰值下 429 实际不触发，故不依赖平台重试；③幂等按 **(scope, content_hash) 数据库唯一键 + INSERT OR IGNORE** 实现（非以 request_id 为键），重试不重复写入，`request_id` 原样回显并落 `source_request_id` 追溯；④评测记忆不被自动回收：30 天 TTL 清扫仅在**进程启动时**执行一次，且只删标记为 eval 且末次写入超 30 天的 scope，评测途中不发 `DELETE /scope`；⑤同步 Add 语义=返回成功前已落库且可立即检索。 |
| 赛道面 | 官方 `track-eligibility`（版本 `version_e2f31aea1ff0`，2026-10-30 读取）：textual / coding / multimodal 三项均 `bound=true`（一份端点登记覆盖三赛道）。配额独立计：textual Smoke `passed`、Full 1/2（第 2 次解锁 2026-11-29，本期作废）；coding Smoke 1/30 已用、`smoke_allowed=true`、`new_full_allowed=true`；multimodal 0/30 未动。**multimodal 本期不可参评**：官方契约要求 Add 可收“文本+Base64 图片”有序内容数组，而冻结版 `models.py` 的内容字段只接受字符串（改代码即破坏“版本一致”）。coding 赛道因 `max_active_jobs=1` 被 textual Full 占住槽位，槽位释放时已过 10/31 材料截止，是否可追加待官方答复。 |
| 运行监控（非自证面） | 监控仅用官网接口 + 无副作用 `/health`。05:12:27Z 起跑后采样：06:28 Add 21.3%（8982/42176）、**failures 始终 0**、`submitted == committed`（9294=9294，无在途丢数）；06:45 总体 25.2%（textual 10480/38183 + streaming 143/3993）；07:21 总体 32.5%（textual 13548/38183 + streaming 175/3993）。分支速率差异巨大：textual ≈1.4–1.8 条/秒，streaming ≈0.9–1.4 条/分钟 → 总时长由 streaming 决定。eval 档 `/health` 按官方 Q21 只返 `{"status":"ok"}`，并发计数不外露（合规代价，非缺陷）。 |
| `/health` 负载相关延迟（2026-10-30 境内公网实测） | 空闲窗口（受理前 04:43Z）4 连探 0.198–0.255s 全 200；Full 高峰期复测：`GET /health` 仍返 200，但**首字节 20.1–46.4s**（TCP 建连稳在 0.092–0.134s，可排除链路因素）。同时刻对照：格式非法体的 `POST /add` 短 0.22s 返 422（不进线程池），带合法体的无 key / 错 key `POST /add` 均返 401 但首字节 2.9s / 14.2s。归因（推断，未做服务端插桩）：端点均为同步 `def` 而进 anyio 线程池，Add/Search 满额在途时健康检查与鉴权拒绝路径需排队取槽位。**未因此产生任何失败**（failures 0，契约校验未报错）。 |
| 提交时端点状态 | 受理前刚跑完一次公网契约自测（`scripts/smoke_local.py --scheme bearer --top-k 100`）：SMOKE OK，全部 [OK]。评测进行中不再向公网发 Add/Search 探针（会写脏数据并抢并发额度），只保留无副作用的存活观察。 |
| 并发申报 | Add 64（契约上限）/ Search 128，textual 另加 `top_k=100`；均已通过 Smoke 实测背书。 |
| coding Smoke | `teval_2b0ad2fb3f5d3e1a`（`coding_model=flash`，64/128）**已主动取消**（`status=canceled`、`error=null`，2026-10-30T04:43:32Z）：卡 seed 0/1 共 31 分钟零进度，同窗口我方公网 4 连 `/health` 0.198–0.255s 全 200、`/search` 200、`DELETE` 200 → 卡点属平台侧。取消原因：`max_active_jobs=1` 且活跃态含 `running`，挂死作业会占住唯一槽位挡住 textual Full；`/resume` 服务端拒绝（“only failed or canceled … can be resumed”），所以唯一自愈手段是 `/cancel`。coding 额度 29/30 保留、冷却至 05:11:17Z。 |
| 参赛者档案 | `POST /access/profile` 不存则不能提交，已于 2026-10-30 填报并验证落库：Framework name（公开）`Cortexa — 零模型确定性记忆检索系统`（未改）/ Affiliation `Independent Developer` / Advising teacher(s) `None` / Team name 与 Team members 留空——**参评主体为个人开发者，无机构、无指导教师（如实）**。 |
| 不可自助修改 | 申请时的说明框（notes）与 project_url 只有 POST 创建、无 GET/PUT——旧口径「无 429 限流」**在平台记录上改不掉**（实际运行代码 0caf869 已有硬上限 429+Retry-After，以本卡 §8 为准）。LDBD Key 与 Add/Search Token 由官方签发，不可改。 |
