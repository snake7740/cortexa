# Cortexa — 仿生 AI 记忆系统

> 让 AI 的记忆像人类一样活着：它会成长、会巩固、会遗忘、会自我完善。

> **口径说明（R6，2026-09-06）**：成长/巩固/遗忘类仿真机制（FSRS 巩固调度、
> Dreaming Cycle 等）是**真实使用与研发中**机制，其中 FSRS 检索调制在首次评测
> （无复习史）为中性、不构成参赛提分宣称；参赛口径（能力/开关/阶段/证据）以
> docs/mechanism-status.md 为准。

Cortexa 是面向 [Agent Memory Leaderboard (AML)](https://agentmemoryleaderboard.ai/) 挑战赛的
长期记忆系统。系统只暴露两个记忆操作——**Add**（写入记忆）与 **Search**（返回记忆证据），
回答生成与评分由平台统一完成。

## 快速开始

```bash
# 本地安装
pip install -e .
uvicorn cortexa.server:app --host 0.0.0.0 --port 8000

# 或 Docker（学术·代码路线：平台按 Docker entrypoint 构建部署）
docker build -t cortexa .
docker run -p 8000:8000 -v cortexa-data:/app/data cortexa
```

健康检查：`GET /health`（含 `search_p95_ms / add_p95_ms` 与红线自证标志
`search_within_budget / add_within_budget`，声明即执法）。

## API 契约（AML 官方协议 · 2026-09-06 对照官方 API Guide 对齐，golden 见 `tests/golden/official_aml_contract.json`）

| 端点 | 请求 | 响应 |
|------|------|------|
| `POST /add` | `request_id` + `messages[{role, content, timestamp?}]` + `user_id` + `session_id` | `{success: true, request_id, user_id, session_id}` —— 三 ID 与请求**逐字节回显**；**同步语义**：返回 200 前全部持久化且立即可检索 |
| `POST /search` | `query` + `options?` + `user_id` + `top_k`（正式外部评测固定 **100**） | `{data: [{id, content, score?, created_at?}]}` 按相关性**降序**；无相关记忆返回空数组；**绝不包含 answer**（只返回证据） |
| `GET /health` | 无需鉴权，任意 2xx 即正常（官方默认检查 Add 同源 /health） | 状态 + P95/红线自证 + 部署口径自证（`scene/journal_mode/api_key_required/novel_adapter_enabled`） |
| `DELETE /scope/{scope}` | 运维清理（30 天 TTL 之外的人工兜底） | — |

检索以 `user_id`（内部统一叫 `scope`）**精确隔离**——平台只按该字段存取，跨 user_id 返回记忆属违规；
`session_id` 仅用于分组。官方未声明字段（metadata/filters/rerank 等）一律不发送。
契约格式错误会**立即失败**（Add 缺 success=true 或三 ID 回显、Search 缺 data 或缺 id/content）。

## 配置

复制 `.env.example` 为 `.env`。关键项：

| 变量 | 说明 |
|------|------|
| `CORTEXA_LLM_MODEL` | 固定 `gpt-4o-mini`（官方硬规则：Add/Search 阶段唯一允许模型，平台复现验证） |
| `CORTEXA_EMBEDDING_PROVIDER` | `hash`（离线确定性）/ `openai`（自动按 `EMBEDDING_DIM` 降维，两模式库可共存） |
| `CORTEXA_API_KEY` | 本服务鉴权（官方 Authentication 两种方式之一），留空则不鉴权 |
| `CORTEXA_JOURNAL_MODE` | 任务流水模式：`app` 全量（默认，真实使用）/ `eval` 内容脱敏（参赛评测，合规"评测数据不留日志"）/ `off` 不记录 |
| `CORTEXA_SCENE` | 场景 schema：`novel`（默认，真实使用）/ `aml`（**参赛必设**：治理按 aml schema 生效）/ `code` / `custom` |
| `CORTEXA_NOVEL_ADAPTER_ENABLED` | 领域适配器开关（参赛必须 `false`；Docker 已固化） |

## 术语表（全项目唯一叫法）

| 术语 | 含义 | 代码标识 |
|------|------|---------|
| 巩固调度器 | FSRS 算法管理记忆强度，用得越多越牢 | `models.FSRSState`（调度器 W2+） |
| 知识拓扑地图 | Wing(领域)→Room(主题)→Cluster(簇)→Instance 四层空间结构 | `MemoryInstance.wing/room`（P2 完整） |
| 规则专属 Wing | 规则类记忆独立存放、永不衰减 | `MemoryType.RULE` + `no_decay`（W2 模块） |
| RARE | 检索后四步重编码写回（异步，不阻塞 Search） | P4 |
| Dreaming Cycle | 空闲期批处理：重放→整合→修剪→发现 | P3 |
| 主动遗忘引擎 | 矛盾覆盖/衰减遗忘/整合吸收 + 安全遗忘条件 | `storage.supersede/forget` 已活；衰减/遗忘引擎 P4（阈值已在 config 标注未接线） |
| 维度地板审计 | 压缩前校验结构维度不丢失 | W2 `modules/dimension_floor.py` |
| scope 隔离 | 官方契约检索隔离键 | `MemoryInstance.scope` |
| 任务流水 | 记忆任务过程留痕（add/search/retcon 事件+孤岛统计），可回放审计；clear_scope 同步清除（合规） | `journal.TaskJournal`（facade `.journal`） |
| 孤岛记忆 | 从未被检索命中的活跃记忆（Obsidian Graph View 启发） | `journal.orphan_stats()`（默认全量扫描） |
| 修正链 retcon | 修正已入库记忆：旧转 SUPERSEDED，新带 `op_type='retcon'`+`supersedes_id`，继承逻辑时间轴 | `facade.retcon()` |
| 实体关系图 | 单表双向边 + link_type + 基数声明（1=受限端：1-m 限 src、m-1 限 dst） | `modules/graph.py`，facade `link/unlink/neighbors/link_history` |
| 实体编辑史 | 单实体三源合流：状态线 + retcon 修正事件 + 边史（Palantir edits history 移植） | `facade.entity_history()` |
| 一致性审计 | 实体归属状态线 + 回跳/跳级 + 物品缺口（本地零 LLM，双时间轴） | `facade.audit()` / `modules/consistency.py` |
| 特征访问通道 | 查询提到已知实体即直取其记忆（实体倒排第三路进 RRF，特征唤起/维度 B 前哨）；空 scope 缺席闸 O(1) 返回空。默认关闭：小说 A/B 实证 Δ=0 且延迟为正，按"实际使用先检验"原则回基线，P4 专名提质后再评估 | `storage.entity_search` + `settings.entity_channel`（可开关） |
| 编号精确通道 | 章节锚点精确检索（"第 107 章讲了什么"/"第三十七章"/"Ch 40"，含中文数字解析）；小说实证 0/4 → 4/4，延迟 +3ms | `storage.chapter_search` + `settings.chapter_channel`（可开关） |
| 查体成绩单 | 五科主动自检（地板复验/孤岛/衰减健康度/探针自测/一致性），只评不改，长篇内容例行体检 | `facade.checkup()` / `modules/checkup.py` + `examples/novel_checkup.py` |
| 端点配置文件 | 不绑定供应商：任意 OpenAI 兼容云端（DeepSeek/通义/Kimi/中转）一份 `.env` 接管；优先级：构造参数 > .env 自动探测（data_dir→cwd，模板见 `.env.example`）> 无前后缀环境变量 > 默认离线 hash | `.env` / `Cortexa(config_file=...)` |

## 作为类库使用（双入口单管线，ADR-0005）

```python
from cortexa import Cortexa

with Cortexa() as cm:
    cm.remember("这个项目的包管理器统一用 pnpm，不要用 npm", scope="my-project")
    print(cm.recall_texts("包管理器用什么", scope="my-project"))
    print(cm.journal.summary(scope="my-project"))   # 做了什么任务、做到哪一步
    print(cm.journal.orphan_stats(scope="my-project"))  # 写入了但从未召回的记忆

    # 治理层（Palantir Ontology 纪律移植，长篇小说 127 章实战验证）
    cm.retcon("my-project", "旧决策原文", "新决策原文", reason="口径修正")  # 修正链留痕
    cm.declare_link_type("my-project", "依赖于", "m-1")                      # 基数声明
    cm.link("my-project", "模块A", "模块B", "依赖于", evidence="ADR-0012")
    print(cm.neighbors("my-project", "模块A"))            # 双向遍历
    print(cm.entity_history("my-project", "模块A"))       # 单实体编辑史
```

真实使用（小说线索一致性 / 代码决策账本 / 学术推理）与 HTTP 服务共用同一
Add/Search 管线，反馈不失真。

### 开发决策账本（dogfooding）

本仓库自身用 Cortexa 记录开发决策（scope=`cortexa-dev`，数据 `~/.cortexa`），规范见 `docs/dev-dogfooding.md`；
随手记：`python examples/dev_log.py remember "决策+依据"`，开工前 `recall` 复习，改口径用 `retcon`。

### 参赛部署（一键合规）

Docker 已固化参赛环境（`CORTEXA_SCENE=aml` + `CORTEXA_JOURNAL_MODE=eval` + `CORTEXA_NOVEL_ADAPTER_ENABLED=false`），docker build && docker run 即合规；
本地起服参赛时请设同一组变量（见 .env.example「参赛必设三键」）。服务启动会自检：journal≠eval 或 scene≠aml 时打醒目 WARN（不静默）；
/health 暴露 `scene/journal_mode/api_key_required` 供自证。30 天数据删除双保险：eval 部署启动自动清扫超 30 天 scope（TTL）+ `DELETE /scope/{scope}` 人工兜底。

## 开发

```bash
pip install -e ".[dev]"
pytest
```

## 里程碑（评测驱动排期）

| 阶段 | 窗口 | 目标 | 实际进度 |
|------|------|------|------|
| W1 契约对齐与骨架 | 8/29-9/4 | Add/Search 全链路可跑、红线不变量测试绿 | ✅ 8/31 提前完成 |
| W2 MVP 管线 | 9/5-9/11 | 规则 Wing / 维度地板 / 双通道检索 / 契约测试 | ✅ 8/31 提前完成 |
| 双闭环实战（额外） | — | 小说 127 章/1737 条记忆可复盘不混乱；Palantir 纪律移植（retcon/links/编辑史） | ✅ 8/31，154 测试绿 |
| W3 评测模拟器 | 9/12-9/19 | 公开数据集本地近似分 + Docker 部署预演 | ✅ 8/31 提前完成（`examples/local_eval.py` 消融裁判 + 容器内 Add→Search 全链路冒烟 + Dreaming 设计稿；审计后实体通道默认关、编号精确通道落地并小说实证 0/4→4/4） |
| 复盘 #4 审计器自修（额外） | — | Ch1-176 全量复盘暴露的三类系统性偏差：等级表补巅峰/圆满变体、模糊修饰降级+异常检测收窄 high 点、物品缺口支持"正名｜别名"代称在场 | ✅ 9/1，160 测试绿 |
| 报名周 | 9/20-9/26 | 提交申请、官方 Smoke 通过（契约 golden 已固化，正式 Top K=100） | — |
| 第 1 次 Full | 9/27-10/10 | 尽早评测、拿七维诊断 | — |
| 定向优化 | 10/11-10/24 | 按诊断短板排序开发机制 | — |
| 第 2 次 Full | 10/25-10/29 | 版本冻结、冲刺 | — |

> 机制/开关/状态单一事实源见 `docs/mechanism-status.md`（含 2026-09-06 批① P0 处置）；契约 golden 见 `tests/golden/official_aml_contract.json`。

## 署名与致谢（AML 开源合规）

本系统为自主设计的原创实现，思想来源披露：

- **FSRS 间隔重复算法**（Jarrett Ye, Free Spaced Repetition Scheduler）——巩固调度器的 DSR 模型与遗忘曲线公式
- **测试效应**（Karpicke & Roediger, Science 2008）——RARE 检索增强重编码的心理学依据
- **MemPalace 空间结构 / A-MEM 动态链接 / LightMem sleep-time**——知识拓扑地图与离线整理的工程借鉴
- 数学约束（维度地板/安全遗忘条件/粘性约束）为本项目对菲尔兹奖工作的**类比迁移**，非严格数学定理
- 评测协议与回答/评分行为参考 AML 官方公开仓库 `AML-memory/agent-memory-leaderboard` 的公开契约文件

License: MIT（见 `LICENSE`）。