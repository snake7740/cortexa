# Q8 packaging proxy matrix（2026-09-06）

> 本地代理。语料：LME-S 21 题样本 + InMind 125 会话；top_k=20。无 judge——
> 本矩阵只给成本/覆盖代理，不给"更好"结论。

| corpus | pkg | hit@1 | hit@5 | tok/query(base) | tok/query(pkg) | 增量 | buckets(hit) |
|---|---|---|---|---|---|---|---|
| lme21 | raw | 0 | 5 | 7513 | 7513 | - | 9 |
| lme21 | pm1 | 0 | 5 | 7513 | 11108 | +48% | 9 |
| lme21 | pm2 | 0 | 5 | 7513 | 17920 | +139% | 9 |
| lme21 | raw-atomic | 0 | 5 | 7513 | 11108 | +48% | 9 |
| inmind | raw | 87 | 94 | 13.0 | 13.0 | - | 94 |
| inmind | pm1 | 87 | 94 | 13.0 | 17.5 | +35% | 94 |
| inmind | pm2 | 87 | 94 | 13.0 | 17.5 | +35% | 94 |
| inmind | raw-atomic | 87 | 94 | 13.0 | 17.5 | +35% | 94 |

结论：
1. **membership/order 不变量成立**（分桶逐字节一致）——包装只改变槽内文本，如设计。
2. 代价显著且无界风险：LME21 长消息下 ±2 使送答文本 +139%（7513→17920 chars/q）；
   若未来启用必须加有界 cap（迁移 B1 的 EXPAND_MAX_FILL 思路）。
3. 收益面未知：无 judge 时包装无法裁决；raw-atomic(=RAW+前1)与 pm1 在此代理等价。
4. 判定：维持实验档（默认 raw 零变化）；正确顺序 = judge(key) 先行再裁决包装。
