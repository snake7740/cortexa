# Cortexa — 零模型确定性记忆检索系统

> 核心主张：不依赖任何生成模型，也能把长期记忆的检索做扎实、做可复现。

> **口径说明（2026-10-29 修订）**：本系统的参赛版本是**纯检索系统**——Add/Search 全链路
> 不调用生成模型，嵌入为确定性特征哈希，得分来自词法检索、多通道 RRF 融合与同会话
> 邻接扩展。早期以"仿生"（会成长/巩固/遗忘/自我完善）为核心卖点的机制属于真实使用与
> 研发侧，已被实测降级为**非核心优点**：FSRS 保留度调制在评测档无复习史、属中性；
> Dreaming Cycle 与衰减遗忘引擎未实现；RARE 重编码、领域适配器等默认关闭。
> 参赛口径（能力/开关/证据）以 `docs/METHOD_CARD.md` 为单一事实源。

Cortexa 是面向 [Agent Memory Leaderboard (AML)](https://agentmemoryleaderboard.ai/) 挑战赛的
长期记忆系统。系统只暴露两个记忆操作——**Add**（写入记忆）与 **Search**（返回记忆证据），
回答生成与评分由平台统一完成。

## 快速开始

```bash
# 本地安装
pip install -e .
uvicorn cortexa.server:app --host 0.0.0.0 --port 8000

# 或 Docker（第二期官方不接受"只交仓库由平台代部署"，须自行部署到公网可访问地址）
docker build -t cortexa .
docker run -d -p 8000:8000 -e CORTEXA_API_KEY=<k> -v cortexa-data:/app/data cortexa
```

健康检查：`GET /health`。`app` 档返回 P95 与红线自证标志
（`search_within_budget / add_within_budget`，声明即执法）；参赛 `eval` 档按官方 Q21
收敛为最小面 `{"status": "ok"}`（无鉴权端点不暴露密钥/模型配置/库地址）。

## API 契约（AML 官方协议 · 2026-10-29 对照现行 API Guide 与赛事页核实，golden 见 `tests/golden/official_aml_contract.json`）

| 端点 | 请求 | 响应 |
|------|------|------|
| `POST /add` | `request_id` + `messages[{role, content, timestamp?}]` + `user_id` + `session_id` | `{success: true, request_id, user_id, session_id}` —— 三 ID 与请求**逐字节回显**；**同步语义**：返回 200 前全部持久化且立即可检索 |
| `POST /search` | `query` + `options?` + `user_id` + `top_k`（正式外部评测固定 **100**） | `{data: [{id, content, score?, created_at?}]}` 按相关性**降序**；无相关记忆返回空数组；**绝不包含 answer**（只返回证据） |
| `GET /health` | 无需鉴权，任意 2xx 即正常（官方默认检查 Add 同源 /health） | `app` 档：状态 + P95/红线自证 + 部署口径自证；`eval` 档（参赛）：仅 `{"status":"ok"}` 最小面 |
| `DELETE /scope/{scope}` | 运维清理（30 天 TTL 之外的人工兜底） | — |

检索以 `user_id`（内部统一叫 `scope`）**精确隔离**——平台只按该字段存取，跨 user_id 返回记忆属违规；
`session_id` 仅用于分组。官方未声明字段（metadata/filters/rerank 等）一律不发送。
契约格式错误会**立即失败**（Add 缺 success=true 或三 ID 回显、Search 缺 data 或缺 id/content）。

## 配置

复制 `.env.example` 为 `.env`。关键项：

| 变量 | 说明 |
|------|------|
| `CORTEXA_LLM_MODEL` | 默认值固定 `gpt-4o-mini`（开源方法榜硬规：Add/Search 阶段唯一允许模型，平台会复现验证）；**参赛评测版本该路径不调用任何 LLM**——规则引擎未命中直接落 FACT，不兜底调模型 |
| `CORTEXA_EMBEDDING_PROVIDER` | `hash`（评测默认：确定性特征哈希，非学习型，零外部调用）/ `openai`（自动按 `EMBEDDING_DIM` 降维，两模式库可共存）。v4 语义通道已按官方口径 250 题成对 A/B 证伪（各项指标均劣于 hash），参赛版不启用 |
| `CORTEXA_API_KEY` | 本服务对外鉴权口令，即提交官网的"记忆系统 Key"；留空=不鉴权，**参赛必须设置** |
| `CORTEXA_JOURNAL_MODE` | 任务流水模式：`app` 全量（默认，真实使用）/ `eval` 内容脱敏（参赛评测，合规"评测数据不留日志"）/ `off` 不记录 |
| `CORTEXA_SCENE` | 场景 schema：`novel`（默认，真实使用）/ `aml`（**参赛必设**：治理按 aml schema 生效）/ `code` / `custom` |
| `CORTEXA_NOVEL_ADAPTER_ENABLED` | 领域适配器开关（参赛必须 `false`；Docker 已固化） |

## 术语表（全项目唯一叫法）

| 术语 | 含义 | 代码标识 |
|------|------|---------|
| 巩固调度器 | FSRS 算法管理记忆强度，用得越多越牢（真实使用侧）；评测档仅作无状态乘性调制 | `models.FSRSState` |
| 知识拓扑地图 | Wing(领域)→Room(主题)→Cluster(簇)→Instance 四层空间结构 | `MemoryInstance.wing/room` |
| 规则专属 Wing | 规则类记忆独立存放、永不衰减 | `MemoryType.RULE` + `no_decay` |
| RARE | 检索后四步重编码写回（异步，不阻塞 Search） | **未实现**，仅设计稿；评测版不启用 |
| Dreaming Cycle | 空闲期批处理：重放→整合→修剪→发现 | **未实现**，仅设计稿；评测版不启用 |
| 主动遗忘引擎 | 矛盾覆盖已活（`storage.supersede`）；衰减遗忘/整合吸收未接线 | `storage.supersede/forget`（衰减阈值已在 config 标注未接线） |
| 维度地板审计 | 压缩前校验结构维度不丢失 | `modules/dimension_floor.py` |
| scope 隔离 | 官方契约检索隔离键 | `MemoryInstance.scope` |
| 任务流水 | 记忆任务过程留痕（add/search/retcon 事件+孤岛统计），可回放审计；clear_scope 同步清除（合规） | `journal.TaskJournal`（facade `.journal`） |
| 孤岛记忆 | 从未被检索命中的活跃记忆（Obsidian Graph View 启发） | `journal.orphan_stats()`（默认全量扫描） |
| 修正链 retcon | 修正已入库记忆：旧转 SUPERSEDED，新带 `op_type='retcon'`+`supersedes_id`，继承逻辑时间轴 | `facade.retcon()` |
| 实体关系图 | 单表双向边 + link_type + 基数声明（1=受限端：1-m 限 src、m-1 限 dst） | `modules/graph.py`，facade `link/unlink/neighbors/link_history` |
| 实体编辑史 | 单实体三源合流：状态线 + retcon 修正事件 + 边史（Palantir edits history 移植） | `facade.entity_history()` |
| 一致性审计 | 实体归属状态线 + 回跳/跳级 + 物品缺口（本地零 LLM，双时间轴） | `facade.audit()` / `modules/consistency.py` |
| 特征访问通道 | 查询提到已知实体即直取其记忆（实体倒排第三路进 RRF，特征唤起/维度 B 前哨）；空 scope 缺席闸 O(1) 返回空。默认关闭：小说 A/B 实证 Δ=0 且延迟为正，按"实际使用先检验"原则回基线，P4 专名提质后再评估 | `storage.entity_search` + `settings.entity_channel`（可开关） |
| 编号精确通道 | 章节锚点精确检索（"第 107 章讲了什么"/"第三十七章"/"Ch 40"，含中文数字解析）；小说实证 0/4 → 4/4，延迟 +3ms | `storage.chapter_search` + `settings.chapter_channel`（可开关） |
| 查体成绩单 | 五科主动自检（地板复验/孤岛/衰减健康度/探针自测/一致性），只评不改，例行体检 | `facade.checkup()` / `modules/checkup.py` |
| 端点配置文件 | 不绑定供应商：任意 OpenAI 兼容云端（DeepSeek/通义/Kimi/中转）一份 `.env` 接管；优先级：构造参数 > .env 自动探测（data_dir→cwd，模板见 `.env.example`）> 无前后缀环境变量 > 默认离线 hash | `.env` / `Cortexa(config_file=...)` |

## 作为类库使用（双入口单管线，ADR-0005）

```python
from cortexa import Cortexa

with Cortexa() as cm:
    cm.remember("这个项目的包管理器统一用 pnpm，不要用 npm", scope="my-project")
    print(cm.recall_texts("包管理器用什么", scope="my-project"))
    print(cm.journal.summary(scope="my-project"))   # 做了什么任务、做到哪一步
    print(cm.journal.orphan_stats(scope="my-project"))  # 写入了但从未召回的记忆

    # 治理层（Palantir Ontology 纪律移植，长篇小说连载实战验证）
    cm.retcon("my-project", "旧决策原文", "新决策原文", reason="口径修正")  # 修正链留痕
    cm.declare_link_type("my-project", "依赖于", "m-1")                      # 基数声明
    cm.link("my-project", "模块A", "模块B", "依赖于", evidence="ADR-0012")
    print(cm.neighbors("my-project", "模块A"))            # 双向遍历
    print(cm.entity_history("my-project", "模块A"))       # 单实体编辑史
```

真实使用（小说线索一致性 / 代码决策账本 / 学术推理）与 HTTP 服务共用同一
Add/Search 管线，反馈不失真。

### 开发决策账本（dogfooding）

本仓库自身用 Cortexa 记录开发决策（scope=`cortexa-dev`，数据 `~/.cortexa`）：
`cm.remember(...)` 记决策与依据，开工前 `recall` 复习，改口径用 `retcon`。

### 参赛部署（一键合规）

Docker 已固化参赛环境（`CORTEXA_SCENE=aml` + `CORTEXA_JOURNAL_MODE=eval` + `CORTEXA_NOVEL_ADAPTER_ENABLED=false` + `CORTEXA_ASYNC_ENHANCE_ENABLED=false` + `CORTEXA_RULE_LLM_FALLBACK=false` + `CORTEXA_ADJACENCY_ENABLED=true`），docker build && docker run 即合规，并自带 HEALTHCHECK；
本地起服参赛时请设同一组变量（见 .env.example「参赛必设四键」）。服务启动会自检：journal≠eval 或 scene≠aml 时打醒目 WARN（不静默）；
/health 暴露 `scene/journal_mode/api_key_required` 供自证。鉴权按官方四方案全收：空 key=无鉴权（**参赛不允许裸奔，必须设 key**）；设 `CORTEXA_API_KEY` 后 `Authorization: Token <k>` / `Authorization: Bearer <k>` / `X-Api-Key: <k>` 三头任一。
30 天数据删除双保险：eval 部署启动自动清扫超 30 天 scope（TTL）+ `DELETE /scope/{scope}` 人工兜底。

运行限制（如实，已写入提交说明）：单实例单进程。契约面有在途准入阀：**超软上限（默认 64）先排队（最多
2000ms）后照常返 200，只有在途 ≥ 硬上限（默认 512）才返 429 + Retry-After**；未鉴权请求不占配额。
Search 另有请求内 deadline（默认 **5000ms**）与返回预算（默认 60000 token 估算）。deadline 只对齐官方硬约束
（单请求最长 30 分钟、计分维度不含延迟）；6000 条语料实测 p50 41.0 / p95 83.9 / max 110.9ms，故 5000ms 只做
真卡死兜底（到点只跳邻接扩展等加料步、仍返 200），正常负载零行为变化；返回预算相对实测返回量留 ≥2 倍余量。
阈值可用 `CORTEXA_ADMISSION_*` / `CORTEXA_SEARCH_DEADLINE_MS` / `CORTEXA_SEARCH_RETURN_TOKEN_BUDGET` 覆盖，
app 档 `/health` 的 `admission` 计数可查。

**部署后、点官网 Smoke 前先自测**（标准库，零依赖，对任何 URL）：

```bash
python scripts/smoke_local.py https://<你的公网域名> --key <Memory System Key>   # SMOKE OK 再去官网申请
```

跑 Health→Add→Search→鉴权负例→清理，逐项校验三 ID 回显 / data≤top_k / id&content 非空 / 同步写入可见，避免用官方 smoke 配额（每小时 1 次）调低级错误。提交材料（方法说明/模型声明/复现步骤/思想来源）见 `docs/METHOD_CARD.md`。

## 开发

```bash
pip install -e ".[dev]"
pytest
```

## 里程碑（评测驱动排期）

| 阶段 | 窗口 | 目标 | 实际进度 |
|------|------|------|------|
| 契约对齐与骨架 | 8/29-9/4 | Add/Search 全链路可跑、红线不变量测试绿 | ✅ 8/31 提前完成 |
| MVP 检索管线 | 9/5-9/11 | 规则 Wing / 维度地板 / 多通道检索 / 契约测试 | ✅ 8/31 提前完成 |
| 双闭环实战（额外） | — | 小说连载全量记忆可复盘不混乱；Palantir 纪律移植（retcon/links/编辑史） | ✅ 8/31（当时 154 用例） |
| 评测模拟器 | 9/12-9/19 | 公开数据集本地近似分 + Docker 部署预演 | ✅ 8/31 提前完成（`examples/local_eval.py` 消融裁判 + 容器内 Add→Search 全链路冒烟；审计后实体通道默认关、编号精确通道落地并小说实证 0/4→4/4） |
| 审计器自修（额外） | — | 全量复盘暴露的三类系统性偏差：等级表补巅峰/圆满变体、模糊修饰降级+异常检测收窄 high 点、物品缺口支持代称在场 | ✅ 9/1（当时 160 用例） |
| 报名与首测（原计划） | 9/20-10/10 | 提交申请、官方 Smoke、尽早拿七维诊断 | ✗ 未执行（9/07–10/27 项目停摆） |
| 定向优化（原计划） | 10/11-10/29 | 拿诊断后第 2 次 Full | ✗ 按官方规则每 Key 每赛道 Full ≤2 次且间隔 30 天，本期实际仅 1 次 |
| **第二期冲线（现行）** | **10/28-10/31 23:59** | 契约补齐 → 公网自托管 → 公开仓固定 commit → 官网 Smoke → 交材料 | ◔ 10/28 契约补齐 + 自测脚本 + METHOD_CARD 就位；10/29 Docker 构建+HEALTHCHECK+全链路冒烟通过，同会话邻接扩展进入参赛默认档（官方口径 250 题本地成对 A/B：命中 94→129，逐题 35 升 0 回退）；公开仓 snake7740/cortexa 快进至 commit `080dbc0`；**质量门当前 337 用例全绿**；10/29 核到硬约束：Eval Key 提交申请后**次日 19:00 (UTC+8) 发放**；10/30 受理前补运行时护栏（在途准入阀 + Search deadline + 返回预算；retrieval-gate 无回退），固定 commit 更新至 `fb56f83`；10/30 公网并发取证发现并发正确性 P0（journal 与 store 共用同一 SQLite 连接但各持一锁，c=16 实测 500 并静默丢记忆），且实测确认 deadline 原值 200ms 对齐的是内部红线而非官方约束（官方单请求 30 分钟、无延迟计分维度），同批修复 + 重定为 5000ms，固定 commit 更新至 `0caf869`（337 用例全绿，retrieval-gate 无回退）；10/30 公网镜像已按 `0caf869` 重建重部署，取证 c=16/24 共 168 个并发 Add **零非 200**、`:8000` 与 `:80` 双端点 `SMOKE OK`；剩官网 Smoke 与 Full 提交待执行 |
| 唯一一次 Full | ≤ 11/04 23:59 前受理完成 | 版本冻结，受理后不可换版 | — |

> 参赛口径（机制/开关/证据）单一事实源见 `docs/METHOD_CARD.md`；契约 golden 见 `tests/golden/official_aml_contract.json`。

## 署名与致谢（AML 开源合规）

本系统为自主设计的原创实现。思想来源按**评测版是否实际依赖**分组披露：

**评测版实际依赖（分数真正来源）**

- **Reciprocal Rank Fusion**（Cormack, Clarke & Buzykala, 2009）——多通道检索融合
- **InvMem / ActiveMemoryIndex**（AML 首期开源方法榜第 1/3 名，官方公众号公开机制说明）——**同会话邻接扩展**（融合序前 20 种子各补 ±1 时间邻居）为本系统直接借鉴并独立实现，选择题 options 拼接查询亦借鉴其查询构造；这是评测版唯一直接取自榜首的机制
- **FSRS 间隔重复算法**（Jarrett Ye, Free Spaced Repetition Scheduler）——保留度 DSR 模型与遗忘曲线公式（评测档为无复习史中性调制）
- 评测协议与回答/评分行为参考 AML 官方公开仓库 `AML-memory/agent-memory-leaderboard` 的公开契约文件

**真实使用与研发侧（评测版未启用，不构成参赛能力宣称）**

- **测试效应**（Karpicke & Roediger, Science 2008）——RARE 检索增强重编码的心理学依据
- **MemPalace 空间结构 / A-MEM 动态链接 / LightMem sleep-time**——知识拓扑地图与离线整理的工程借鉴
- 数学约束（维度地板/安全遗忘条件/粘性约束）为本项目对菲尔兹奖工作的**类比迁移**，非严格数学定理

License: MIT（见 `LICENSE`）。