# 纯技术缺陷/功能缺失 · 排期修复单（2026-09-06，报名类一律剔除）

> 范围：仅代码/评测/健壮性。依据=7+ 份组织方披露 + 本项目实验负结果与 audit 档案。每项带验收断言与依赖。

## T1 · 检索正确性与召回（功能缺失→默认关原型/实验）

| # | 缺失/缺陷 | 方案 | 验收断言 | 依赖 | 估 |
|---|---|---|---|---|---|
| T1.1 J1 查询规划器 | 无 core_terms/entities/temporal_cues/evidence_needs 解拆；仅 chapter 强信号 | 确定性解析→分字段进通道+evidence-need 有界槽(仿 CHM P1/P4-A)，默认关 | 21 题样本：hit 不劣且 miss→hit 类 > 新 miss 类 | — | M |
| T1.2 F2 写时相对时间锚定 | 相对时间(上周/去年/几个月前)无解析→C 轴全榜空白同病 | 规则集→绝对日期入 event_ts/时间前缀 | 时间类题(5e1b23de 等)evidence rank 不降且救回≥1 | T2.1 判答可后置 | M |
| T1.3 H1 会话级 RRF | 融合仅 turn 级，无 session 聚合项(ReFind cluster boost) | RRF 第二项 r2=session 聚合名次，默认关 | 多会话 fixture 单测绿 | — | S |
| T1.4 B2 soft-supersede | 硬隐藏证据有 2/21 误藏史；AMI 无治理治理第一 | governance_soft 实验开关(降权保留 vs 硬隐藏)，默认硬隐藏 | 治理三分法对照表 + 检索命中双指标 | — | M |

## T2 · 评测/测量（先把尺子做对）

| # | 缺失 | 方案 | 验收 | 依赖 | 估 |
|---|---|---|---|---|---|
| T2.1 judge v1 | 全部分桶/改写/规划结论停在检索侧，无判答层 | refs 六 pipeline answer 模板+gpt-4o-mini 顶替(需用户 key 跑分，代码夹具离线) | 用户环境一轮判答对比 | — | M |
| T2.2 InMind 路由门 | 无隐式关联(路由)测量；我们 channel-miss 大类无正式靶场 | 下载 inmind.jsonl→转换→direct vs indirect(target) recall 基线 | 报告我们 direct% vs target% 差 | 网络 | S |
| T2.3 token 等预算审计 | AMI 指出 top_k 条数漏洞(父块 5.4×内容) | aml_replay --token-cap 截断成对 | token 等预算 A/B 报告 | — | S |
| T2.4 recall_miss 诊断卡 | 分桶有但无逐题原因建议 | 报告升级 miss 原因卡 | recall_miss_*.md 输出 | — | S |

## T3 · 工程健壮与卫生

| # | 缺陷 | 方案 | 验收 | 依赖 | 估 |
|---|---|---|---|---|---|
| T3.1 stress_smoke | 无平台形态(逐 chunk Add+Search 交替)压测；单连接 RLock 吞吐未知 | 2000 条级压测脚本(只观测) | 脚本+报告 | — | S |
| T3.2 config 矩阵/直测缺口(P2-6) | env 优先级矩阵、run_add 直测缺 | 补测试 | pytest 绿 | — | S |
| T3.3 RARE 术语统一(P2-2) | RARE 四步与 RARE-lite 并存易误读 | 文档/注释统一为 RARE-lite(检索后写回) vs RARE(完整四步,P4) | 术语表无歧义 | — | XS |
| T3.4 mechanism-status v3 | 表停在 A1/A2 行，A/B/E/F/G/H/J 大量未收 | 全量刷新 | 表 v3 与档案一致 | — | S |

## 执行顺序（依赖优先，每项过闸门）
1. T2.2 InMind 路由门(数据可自取，先量化路由缺口) → 2. T2.3/T2.4 测量件 → 3. T1.3 H1 → 4. T1.1 J1 → 5. T1.2 F2 → 6. T1.4 B2 → 7. T2.1 judge 夹具 → 8. T3 批