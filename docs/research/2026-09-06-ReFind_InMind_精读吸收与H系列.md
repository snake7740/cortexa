# ReFind & InMind 精读吸收 + H 系列动作（2026-09-06）

> 输入：官方精读文（用户转录）+ 交叉核验：imlrz/ReFind、imlrz/InMind README（本机直读一致）；arXiv 2608.12888 / 2607.24368。

## 1. ReFind 精确实现卡（README 已核验）
- 写入：SQLite 同步（200 前提交）；request_id 幂等、冲突复用拒绝；Add 原文 raw 存储、**按 user_id+session_id 分组、时间戳/chunk 序号排序并配对**（=会话结构恢复的底子）。
- 索引：turn 粒度词法 BM25（停用词+Porter 词干），无 LLM 建索引；no-key bm25 模式仅供 smoke/消融。
- 检索：GPT-4o-mini ReAct 规划 ≤4 轮动作（search_chatrecord top-5 / take_note / finish_search）；命中展开**前后各 2 邻接 chunk**；可用绝对日期时间过滤；跳过已检会话。
- 排序：**会话级 RRF（k=60）：r1=turn 自身名次 + r2=所在 session 聚合名次**（cluster boost）——与我们的 turn 级 RRF 差一个『会话聚合项』。
- 竞赛版：只返回有序证据，刻意省略论文第二阶段生成；METHOD_CARD 公开竞赛特定改动（透明披露实践值得抄）。
- 消融要点（LongMemEval）：agent 多轮 vs 单次在 M 上 −20.4、会话去重 −9.3、上下文窗口 −9.2/−4.9、RRF −3.9/−4.9、时间过滤 −1.9/−4.9——**长历史越依赖迭代与会话线索**；全部组件正向。

## 2. InMind：检索假设的系统性失效与『路由』问题
- 125 专家核验任务×10 生活域；配对=合成个人事实 + direct + indirect + expected bridge。
- 四控分离：Naive recall(存储/直搜失败) / In-context(模型桥接知识) / Target recall(**检索/路由失败**) / Application(端到端)。
- 关键数字：backbone 上下文内间接准确率 84% 但最佳检索系统应用 ≤14.4%；direct recall 最高 100%；8× 嵌入维度无效；minimal always-in-state probe 恢复 68.8% → **失败定位在路由机制（哪些记忆应持续可见），不在存储/模型/维度**。

## 3. 对 Cortexa 的含义与 H 系列动作

| # | 动作 | 依据 | 形态 |
|---|---|---|---|
| H1 | 会话级 RRF 第二项（cluster boost，k=60 语义下 r2=session 聚合名次） | ReFind 排序层 | 实验旋钮默认关；多会话 fixture 单测 + 官方 Smoke 长上下文验证 |
| H2 | 时间过滤（绝对日期范围，按 query 时间线索） | ReFind 控制③ | 与 F2 相对时间解析合并为一个时间能力工作包 |
| H3 | METHOD_CARD 式参赛披露文档（竞赛特定改动/保留组件清单） | ReFind 实践 | 报名前随 golden 一并成稿 |
| H4 | RULE Wing=‘always-in-state’ 原语认知：我们 no_decay+boost 已是 InMind 68.8% 恢复路径的最小实现——把‘持续可见记忆’从规则扩展为显式路由策略需成对实验（勿盲扩） | InMind | 研究定位更新（文档），不扩代码 |
| H5 | InMind 任务集接入回放器作关联/路由门（125 题、需配对桥数据） | InMind repo 有 dataset jsonl | 数据可得后 B4 升级为 H5；当前 data 下载可行（hf/github） |

## 4. 一句话
ReFind 的会话结构恢复与『把决定延迟到查询期』与我们方向同源；InMind 则把我们的 channel-miss 大类升格为独立科学问题（路由）——Cortexa 的 RULE Wing 恰是『持续可见记忆』的最小种子，H1/H2 是把 ReFind 排序/时间件补上、H5 是给关联路由装正式靶场；全部改动默认关、成对验证。