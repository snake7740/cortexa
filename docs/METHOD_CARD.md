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
| 评测 commit | `080dbc06a58d1056deb4d1c7f8e46b883ac29cfc`（**代码冻结点**，公网端点镜像即由它构建；受理后不变）。其后的提交仅修订 README 与本卡的表述，**不改任何代码、配置、测试与镜像内容**——可执行 `git diff 080dbc0 HEAD -- cortexa tests scripts examples Dockerfile requirements.txt pyproject.toml .env.example` 复核，结果应为空（本卡提交时实测为空）。 |
| 公网端点 | `http://47.79.226.71:8000`（Add `/add`、Search `/search`、Health `/health`）；同容器另绑标准端口 `http://47.79.226.71`（80）作为后路，两端点同一实例同一数据卷。由评测 commit 构建的镜像单容器常驻，容器 `--restart unless-stopped`。契约自测（`scripts/smoke_local.py` 对该公网地址）全部通过：Add 200 / Search 命中 / 无 key 与错 key 均 401。 |
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
4. **幂等**：同 `request_id` 重放返回 200/`success:true`，不重复写（官方重试语义）。
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
git clone https://github.com/snake7740/cortexa.git && cd cortexa && git checkout 080dbc06a58d1056deb4d1c7f8e46b883ac29cfc
docker build -t cortexa . && docker run -d -p 8000:8000 -e CORTEXA_API_KEY=<k> -v cortexa-data:/app/data cortexa
python scripts/smoke_local.py http://127.0.0.1:8000 --key <k>     # 契约自测
python scripts/check.py                                           # compileall + ruff + retrieval-gate + pytest
```

Docker 镜像固化 `CORTEXA_SCENE=aml / JOURNAL_MODE=eval / NOVEL_ADAPTER_ENABLED=false / ASYNC_ENHANCE_ENABLED=false / RULE_LLM_FALLBACK=false / ADJACENCY_ENABLED=true`；外部仅需注入 `CORTEXA_API_KEY`（及可选的 v4 嵌入三件套）。

## 5. 本地质量门（非官方成绩，不可换算）

`scripts/retrieval_gate.py` 对 `tests/golden/retrieval_baseline.json`：hit@1 0.909 / hit@5 1.000 / MRR 0.939 / para_direct 0.544（2026-10-29 复跑 PASS）；pytest 323 用例绿。邻接扩展上游数据成对回放（250 题，本地代理口径）：命中 94→129，逐题 0 回退。
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
  请求走 401，不占配额。Search 另有请求内 deadline（默认 200ms，与 P95 红线同值，到点
  只跳「邻接扩展」这类加料步、不动召回源）与返回预算（默认 60000 token 估算，同官方
  Q18「低优先级先剪」从尾部自剪）。两项在实测负载（Search p95 ≈10ms）下永不触发，
  因此评测返回内容与旧版逐字节一致。阈值均可以 `CORTEXA_*` 环境变量覆盖；app 档
  `/health` 的 `admission` 与 `enhance_stats` 计数为运行时自证（eval 档按 Q21 不开信息面）。
- 本期（10/31 前）只有 1 次 Full 机会，无官方诊断回路，优先级定在"契约零错 + 稳定在线"而非分数冲刺。
