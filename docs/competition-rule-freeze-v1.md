# Competition Rule Freeze v1（一问单，2026-09-06 建；2026-10-28 按官方现行 api-guide 回填）

> 用途：把"我们掌握的官方规则"与"第二期细则落地前必须问清的问题"固化为一页，
> 所有后续工程只依赖本单 + 官方原文（不再逐次改解释口径）。
> **2026-10-28 回填**：第 2 节 10 格依据 https://agentmemoryleaderboard.ai/api-guide（现行）与
> /competition 页逐格填写；凡官方未明示的格标注「未明示」并给默认口径。

## 0. 第二期硬时间（UTC+8，/competition 页）
| 节点 | 时间 | 含义 |
|---|---|---|
| 滚动接入 | 2026-09-20 00:00 起 | 自托管公网端点 + 官网申请评测即可 |
| **材料截止** | **2026-10-31 23:59** | 端点/仓库 commit/方法说明提交止 |
| 评测停止 | 2026-11-04 23:59 | 此后不再受理/运行 Full |
| 成绩公布 | 11 月中旬 | — |

## 0.1 第二期新增硬规（本单建立时未知，10-28 核得）
| 项 | 口径 | 工程含义 |
|---|---|---|
| 开源方法榜模型限制 | **Embedding 须 text-embedding-v4；LLM 组件须 gpt-4o-mini；Reranker 不限** | 语义通道若开只能用 v4（百炼 compatible-mode）；DeepSeek/OpenAI embedding 违规；默认 hash 嵌入=无嵌入模型，合规且需在方法卡明示 |
| 鉴权 | none（仅 smoke）/ `Authorization: Token` / `Authorization: Bearer` / `X-Api-Key` | 服务端三头全收（server.require_key，10-28 已补 Token） |
| Health | 无鉴权 GET，任意 2xx | 已满足 |
| Add 响应 | `success:true` + request_id/user_id/session_id **逐字节回显**；202 仅在绑定状态地址时允许 | 已满足（同步 200） |
| Search 响应 | `data[]` ≤ top_k（正式固定 100），条目 id/content 必填，score/created_at 可选；**超量=契约错误不截断** | 已满足（服务端按 top_k 截断） |
| 文本分段 | 每 Add 20 条消息或 2000 词 | 单请求体量上界，Add 预算按此压测 |
| 单请求时限 | 最长 30 分钟 | 远超现 Add P95；不构成约束 |
| 重试 | Add 失败重试沿用**同 request_id**；409 不算成功 | 幂等已实现（同 request_id 重放 200） |
| 部署形态 | **不接受仅交仓库/Docker 由官方代部署**；必须自托管公网 HTTP(S) | 用户必做项：云主机 + 域名/HTTPS |
| 隐私 | 评测数据 30 天内删除；不得记录不必要请求正文 | eval 日志模式 + TTL 清扫 + DELETE /scope 已实现 |

## 1. 已核固定件（官方原文/镜像，不再重问）
| 项 | 口径 | 来源 |
|---|---|---|
| 协议 | Add/Search 两操作；Search ≤100 证据只返证据 | agent-memory-leaderboard 契约仓库 data/ |
| 契约字段 | user_id / data / content / created_at / TopK100（现行规范） | api-guide（契约迁移已全链路） |
| 判官固定 | Answer+Judge = gpt-4o-mini 系；typo 容忍；prefer most recent；列表 no-extras；时间粒度严格 | data/ 六 pipeline 判词 |
| 维度 | A 显式 / B 多跳 / C 时间事件 / D 治理(含删除合规) / E 个性化 / G 上下文学习 / H 认识论安全 | README_CN |
| 红线 | user_id 精确隔离；评测数据 30 天删除；评测内容不留日志；禁跨样本共享状态；禁硬编码公开题；学术榜需代码/commit/可复现 | README_CN + competition |
| 赛程 | 二期 2026-09-20 00:00 (UTC+8) 开放滚动接入至 10/31；成绩 11 月中旬；奖金 15 万(CSIG) | agentmemoryleaderboard.ai/competition |
| 本地不可换算 | 凡"本地/检索侧"数字非官方成绩 | 项目纪律 |

## 2. 待官方细则确认（一问单，2026-10-28 回填）
| # | 问题 | 为什么关键 | 回答（来源：api-guide 现行版） | 影响面 |
|---|---|---|---|---|
| 1 | Light(Smoke) 评测是否面向 Academic Methods 开放？ | 决定本地/官方闭环形态 | **是**。smoke 对所有拿到 Eval Key 的申请者开放，不分榜 | 工作流/预算 |
| 2 | Light(Smoke) 可提交多少次？ | 决定能否"有限外部校准" | **每小时 1 次；每赛道累计 ≤30 次** | Q4 方法学 |
| 3 | Light/Full 是否固定私有题集？ | 决定多轮对比方差口径 | 未明示题集是否轮间相同；smoke 为公开子集抽样、full 为私有全集。默认按"同集"处理，不做跨轮方差推断 | Q4/实验设计 |
| 4 | 回执是否分维度？ | 决定失败归因粒度 | 未明示逐维回执格式；榜单按赛道/数据集公布。默认：以本地七维消融为归因主源 | 归因闭环 |
| 5 | 一轮评测后能否改版本再交？ | Full 若一锤定音则本地为主 | **Full 受理后不得换版本/撤回**；smoke 可反复 | 提交流程 |
| 6 | Full 提交次数与冷却？ | 资源分配 | **每 Key 每赛道 ≤2 次，第 2 次须在首次完成 30 天后** → 本期（10/31 截止）实际 **只有 1 次 Full**；Full 耗时 0.5–2 天 | 排期 |
| 7 | Full 是否绑定固定 commit/镜像？ | 复现审查要求 | **是**。开源组须公开仓库 + 固定 commit + 来源披露；受理后版本冻结 | 工程冻结 |
| 8 | 同一 sample 内 Search 历史可否影响后续 Search？ | 决定会话内学习是否可做 | 未明示禁止，但"禁跨样本共享状态"为红线。默认维持 §3：Search 纯函数、不依赖查询历史 | 红线边界 |
| 9 | 时间戳粒度/时区？ | 时间件全部依赖 | **消息级可选 `timestamp`，Unix 毫秒**（无时区概念）；服务端 created_at 返 ISO8601Z | F2/R4/RAR |
| 10 | 官方 Smoke 如何申请/规模/何时？ | 契约/运行时验证 | 官网申请评测 → 拿 Eval Key → 填自托管端点 + 鉴权方式 → 点 smoke；即时排队，公开子集小样 | Smoke 排期 |

## 3. 现行默认口径（§2 回填后仍成立）
- Search = deterministic f(user_id, persisted memory, current query)；**无**查询历史学习/touch 状态依赖（参赛档关 touch）。
- 官方评测 = 外部验证（本期只有 1 次 Full）；本地 = 主实验闭环。
- 全部检索旋钮默认关；每项进默认需成对实验 + retrieval-gate。
- 开源榜若开语义通道**只能** text-embedding-v4；否则保持 hash（无嵌入模型）并在 METHOD_CARD 明示。
- 填表完成状态：10 格全部有出处回答（3/4/8 为"官方未明示 + 默认口径"），与 §1 无矛盾 → **本单关闭**。
