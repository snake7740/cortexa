# A3 成对基线：hash@512 vs minilm@384（LongMemEval-S 子集，本地代理）

> 日期 2026-09-06。口径：LongMemEval-S 500 题中随机抽 40 → 短句证据过滤后 21 题（21 会话 / 6120 消息；每会话取证据轮前 ≤1200 条消息的截断语料）。
> 证据 = 内容含 gold 且 ≤600 字符的最短消息轮。判据=证据是否进 Search top_k=5。
> **本地代理结果（非官方成绩）**；官方完整 haystack/判官口径待 Smoke。

## 1. 汇总

| 档位 | hit(21) | hit@1 | hit@5 | fusion-miss | channel-miss | not-in-corpus |
|---|---|---|---|---|---|---|
| hash（默认，512d） | 11 | 4 | 11 | 2 | 6 | 2 |
| minilm（384d） | 8 | 0 | 8 | 3 | 8 | 2 |
| hash --no-governance | 12 | - | - | 2 | 7 | 0 |

## 2. 逐题差异（hash 命中而 minilm 未命中 / 排名反转）

| qid | 题（截断） | hash | minilm | 说明 |
|---|---|---|---|---|
| d52b4f67 | Where did I attend my cousin's wedding? | hit | fusion-miss | vec 也召回但 RRF 稀释出 top5 |
| 9a707b82 | cooking something for my friend...what was it? | hit(rank5) | fusion-miss | 同上：召回在、排序丢 |
| a2f3aa27 | How many followers do I have on Instagram now? | hit | fusion-miss | minilm 召回更弱/更散 |
| a3332713 | total amount spent on gifts | fusion-miss | channel-miss | minilm 连通道召回都丢 |

- minilm hit@1=0：证据虽常进 top5 附近，但 RRF 融合 + 无门控下相关句排在噪声后。
- 两档共同 channel-miss 6（数量/计数/亲属/相对时间类：bass 数、开放麦人数、siblings 总数、Instagram 增量、摄影坊几个月前、亲人事件）——题目词与证据词无语义重叠时 hash 与 MiniLM 都召回不到。

## 3. 结论与动作

1. **维持默认 hash；minilm 不进主路径**——真实英文 LongMemEval 数据复证内部既有结论（行 13/19）：朴素语义替换/RRF 并入伤 top-1 与总召回；语义通道价值须借候选门控（专名/实体前置）兑现，属 P4 方向，不是本期开关。
2. **失败结构**（hash）：channel-miss 6/21（29%）> fusion-miss 2/21 > 治理隐藏 2/21。与上届 P4 同构：先分桶再优化，主桶在通道层（词法/语义都够不到的数量类与跨表述类），不是排序层。
3. **治理对证据可用性有真实影响**：2/21 证据被 supersede 藏起（governance off 后归零）——AML 场景治理过动候补证据，需逐条核对 subject/slot 前缀启发是否误杀仍在效的陈述（列入治理精度审计）。
4. **工具链就绪**：下载（hf-mirror）→ 转换（短句证据过滤）→ 回放（hash/minilm/governance 开关）→ 成对报告全自动；样本 21 题留档 data/aml-materials（不入 git，凭 convert_longmemeval_s.py 可复现）。

## 4. 局限（诚实声明）
- 截断语料（≤1200 条/会话）≠ 官方完整 haystack；证据为内容子串启发式，短句过滤后仍可能有错标；not-in-corpus 桶含义 = 治理隐藏或窗口外。
- 仅检索侧分桶，无判答/判官；hit@1 以证据轮为真值，非官方答案正确性。