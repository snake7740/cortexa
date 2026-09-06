# 实验卡预注册模板（2026-09-06，Q4 方法学落地）

> 规则：跑实验前先填卡（假设/目标桶/预期/失败与晋升条件），结果出来后对照——
> 抑制"看到结果再解释"；单变量；Frozen Champion + One Challenger；闸门复跑。
> 模板化自：外部复核 v3 §8/§16 + 本仓库成对纪律（账本行 32/34/43 等）。

## 实验卡

```
Experiment ID      : <代号>-NNN（如 RAR-002、PACK-001、OPTS-A）
Hypothesis         : <一句话可证伪假设>
Target Dimension   : <A/B/C/D/E/G/H 或 检索侧指标>
Control            : <冻结基线档（label/top_k/provider/数据全注明）>
Treatment          : <单变量改动（哪个开关/参数）>
Dataset            : <题集与规模；本地代理必须标注口径>
Target Bucket      : <期望改善的题桶：如 temporal-conflict / current-state>
Expected Gain      : <目标桶上的量化预期>
Expected Neutral   : <预期无变化的桶>
Possible Risk      : <可能受损的桶/维度>
Primary Metric     : <一个主指标（决定晋级）>
Secondary Metrics  : <hit@1/hit@5/列表污染率/False Conflict 率/tokens-query…>
Regression Guard   : <retrieval-gate 全项 + 关键桶阈值，掉>2% 即红>
Latency Budget     : <search p95 红线内?>
Determinism        : <两次同参跑结果一致?>
Pre-registered At  : <填卡时间>
Result             : <对照实测：目标桶Δ、回归闸、是否达预期>
Decision           : <进默认(带闸) / 维持关 / 证伪并归档>
```

## 已登记实验卡示例

### RAR-001（recency 仲裁冲突簇实验，v3 §3.3 映射）
- Hypothesis：current-state 类问题在"冲突簇读时按 event_ts 重排（最新在前）"下受益。
- Control：baseline RRF；Treatment：RAR reorder（+ 可选 keep-one-predecessor 包装档）。
- Dataset：Temporal-Conflict 集（多题语料落地后）；本地先 21 题+InMind 分桶。
- Regression Guard：列表题污染率 ≤ baseline；retrieval-gate 全项。
- 现状：RAR 原型落地默认关（recency_arbitration.py v2 冲突键）；Treatment 2/3 变体待语料。
