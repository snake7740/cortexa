# temporal_conflict_eval_v1 · 裁决记录与解读（2026-09-06）

> 一套固定尺子裁决四个待决件：F2 日历锚定 / R4 年份过滤 / RAR v2 重排 / C4 keep-predecessor。
> 语料：examples/eval_data/temporal_conflict_eval_v1.jsonl（生成器 examples/gen_temporal_eval.py，
> 确定性无随机）；运行器 examples/temporal_eval.py（5 配置，governance 关=全保留 AMI 路线，top_k=20）。
> 口径：全部为本地代理结果（非官方成绩）。

## 1. 语料（v1 = adversarial synthetic；L1 真实语料=v1.1 待 LME-S 全量时间题筛选）
- convs=45，questions=89：T1 28（current-state）/ T2 16（历史-事件年）/ T3 12（相对日历窗）
  / T4 13（顺序 before-after）/ T5 10（当前列表+历史列表）/ T6 10（False-Conflict decoy）。
- 每会话含 statements truth 表（content→subject/attr），簇归因用。
- 日期边界覆盖：02-28/02-29（闰年）/01-31/03-31/12-31 跨年、跨月、月底、N-months-ago 数值形态。
- 语义约定：消息 timestamp=事件成立时刻；状态-区间（valid_from/valid_to）未建模——
  T2 用"事件年"问法（"in 2025 搬到哪"），"2025 年时住在哪"是已登记缺口（需状态区间）。

## 2. 结果（question-level hit@1，全证据进 top_k 才算 hit；list-stale=当前列表题返回旧版语句数/题数）
| 配置 | T1 | T2 | T3 | T4 | T5 | T6 | 总@1 | false-conflict | list-stale |
|---|---|---|---|---|---|---|---|---|---|
| C0 baseline | 16/28 | 16/16 | 4/12 | 10/13 | 3/10 | 10/10 | 59/89 | 0.0 | 6/3 |
| C1 +F2 | 16/28 | 16/16 | **6/12** | 10/13 | 3/10 | 10/10 | **61/89** | 0.0 | 6/3 |
| C2 +F2+R4 | 16/28 | 16/16 | 6/12 | 10/13 | 3/10 | 10/10 | 61/89 | 0.0 | 6/3 |
| C3 +RAR | **27/28** | **0/16** | 4/12 | **0/13** | 3/10 | **0/10** | 34/89 | 0.0 | 6/3 |
| C4 +RAR keep-prev | **27/28** | **0/16** | 4/12 | **0/13** | 3/10 | **0/10** | 34/89 | 0.0 | **3/3** |

## 3. 裁决结论（对应 Q3/Q1 的四问）
1. **F2 日历锚定：有效（方向成立）**。T3 4→6/12（+2），总 59→61，其它桶零变化零伤害。
   生效面=含相对时间线索的查询（last month/上个月/N months ago/last year）；日历 vs 30/365
   近似的差异已被边界题捕捉（02-28 属 2026-02 整月等）。→ 候选：默认关保持，RAR-002 语料
   +判答层复核后评审进默认；零成本（无线索=原样返回）。
2. **R4 年份过滤：本语料中性**。C2==C1：T2 基线词法已 16/16（语料小、候选噪声少），
   R4 必要性无增益证据；防误伤面（年份隔离同 subject 多版本）在更大候选池才显形。
   → 维持默认关，作为规模档候选过滤件；不主动进默认。
3. **RAR 盲目重排：不可进默认（决定性负结果+修复方向）**。C3 让 T1 +11（16→27）但
   T2/T4/T6 全部归零（16/13/10→0/0/0），总 34/89（-25）。机制有效但**缺意图门**：
   current-state 查询受益、历史/顺序/跨年查询被最新在前反噬。→ 下一步实验 RAR-002：
   query-intent gate（current/now/live→仲裁；in Y/first/before/after/historical→不仲裁
   或时间窗/年份过滤先行），预注册后成对。
4. **C4 keep-one-predecessor：版本史压缩有效但不足以单独晋级**。list-stale 6→3（proj
   三版本链 C4 只返最新+最近旧版）；但 T2/T4/T6 仍 0（与 C3 同病）→ 压缩件并入 RAR-002
   意图门一起评估，不单独进默认。
5. **false_conflict_rate = 0**：冲突键 v3（属性短语+值实体+属格防护）在本语料零误簇——
   T6 属格 decoy（Alice 雇主 vs Alice's brother/姐妹）与 Bob 跨属性（residence×employer
   双簇并存）全过。冲突键 v3 判定通过。

## 4. 晋级门（C4 vs C0，预注册）
1 current-state 优于 C0：PASS（27 vs 16）｜2 historical 不低于 C0：**FAIL（0 vs 16）**
3 false_conflict ≤ 上限（以 C0..C3 分布定，现 0.0）：PASS
4 list 污染无新增系统性错误：PASS（3 vs 6）
5/6 A 面 0.544 / 总体 0.909：由 retrieval-gate 负责（本套默认关设置不触碰默认档）
→ **整体不晋级**：RAR 必须加意图门后重新评审（预注册 RAR-002）。

## 5. 阈值记录（按用户建议：不凭感觉定数字，看 baseline 分布）
- false_conflict_rate 上限：本语料全配置 0.0（冲突键 v3 无 FP）→ 上限可暂定 0.02
  （语料扩大后按 error distribution 复核）。
- list-stale：C4 3/3（每当前列表题返 1 旧版=predecessor 本身）；"无新增系统性错误"判定
  = C4 ≤ C3 且 ≤ C0 的一半。

## 6. 预注册下一步（RAR-002：intent-gated recency arbitration）
- Hypothesis：对 current-state 意图查询启用簇内 recency 重排（最新在前）提升 T1，
  对历史/顺序意图查询不启用（或先做年份/窗口过滤），T2/T4/T6 不回退。
- Treatment：intent 分类（零 LLM：current/cue 词表 vs year/before/after/historical 词表）
  → 决定仲裁开关与 keep_prev 档；Control=C2。
- 目标桶：T1↑ 保持 C3 增益（~+11），T2/T4/T6 恢复 C0 水平（16/13/10）。
- 回归闸：T5 list-stale ≤ C2 基线；A 面 gate；确定性。
- 数据：v1 + v1.1（LME-S 时间题）。

## 7. 待办挂账（本套暴露）
- T2 "某年时状态"语义需 valid_from/valid_to（事件时间≠状态区间）——v1 用事件年问法规避，登记为语料 v1.2 扩展。
- R4 必要性需更大候选池语料（噪声场景）裁决。
- 属格实体（Alice's brother）抽取丢失（encoding 只留 [Alice, Microsoft]）——治理/RAR 靠文本属格检测兜底，NER 级修复挂 P4。
