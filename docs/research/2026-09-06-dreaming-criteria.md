# Dreaming Cycle 冗余判据研究稿（2026-09-06 定稿）

> 状态：P3 设计定稿（实现排期 P3）。本稿回答 review-issues P3-1 的硬前置——
> 「需先定 KL 冗余判据再动手」——给出可执行判据、数据结构与触发/验收条件。

## 一、问题：什么算「冗余」必须可判定，否则整理=删错

Dreaming（离线：重放→整合→修剪→发现）若没有明确冗余判据，整理动作要么过度删减
（误杀仍在使用的证据），要么空转（与不做无异）。判据必须：确定性可跑（不引入额外
LLM 面）、可逆（删除前有 ARCHIVED/回收站语义）、按 scope 隔离（真实小说库不动评测库）。

## 二、判据定义（三信号取并集 + 安全门）

对同 scope 内两条 ACTIVE 记忆 A、B，称 B 为 A 的可修剪冗余，当且仅当满足 **任一** 信号：

1. **内容近重复（token-IoU）**：A、B 均非 RULE/no_decay，且
   `|token(A) ∩ token(B)| / min(|token(A)|, |token(B)|) ≥ 0.92`（中文按二元组/整段保守）。
   语义：占位几乎相同——保留 A（更长/更早 event_ts 锚），B 转 ARCHIVED。
2. **同槽状态过期（双时间轴继承已覆盖）**：B 的 subject/slot 与 A 相同且 B.valid_to_ms 早于
   A.valid_from_ms（A 已 supersede 链替代）——该场景本由治理（维度 D）处理；Dreaming 只做
   复核扫描，确保无残留 ACTIVE 但被链替代的孤儿（query 中不出现即确认）。
3. **整合吸收**：B 为 A 的逐字子串（len(B)≥20 且 B in A），且 A 的 consolidations 未含 B 引用
   ——将 B 归档并把 B.id 记入 A 的 absorbed_ids（JSON 扩展，P3 加列）。

**安全门（与遗忘引擎一致，防误删）**：
 - RULE/no_decay/preference 永不被修剪（维度 F 红线）；
 - 候选 B 若曾被检索命中（journal 有 hit 记录）→ 不修剪，仅报告；
 - 每次修剪前写 lifecycle_events（kind=archived_dreaming），同一库保留 30 天回收期表
   （dreaming_archive 表：scope/id/原文/原因/时间），可人工恢复；
 - 只处理 scope 内记忆，绝不跨 scope。

## 三、KL/信息论视角（诚实标注）

早期设计文档建议以 KL 散度衡量语义冗余。定稿结论：KL 需要概率模型（嵌入分布），当前
hash 嵌入的词袋分布对中文近似 KL 噪音大、易误判；故落地用**确定性 token-IoU + 子串**
近似表达「信息增量≈0」，并以「30 天回收期 + 归档不删除」兜住 KL 路线未来的严格化
（P4 换语义嵌入后可升级判据 1 为嵌入余弦 ≥0.95 且记忆类型相同，接口不变）。

## 四、接线点（实现时照此落）

1. `storage`：加列 memories.absorbed_ids（TEXT JSON）；新建 dreaming_archive 表（同 _SCHEMA 风格）；
2. 新模块 `modules/dreaming.py`：`run_dream_cycle(store, settings, scope)` 返回 {scanned, pruned, archived, report}，
   三信号按 §二实现；触发 = 显式 CLI/服务低峰钩子（不做常驻后台）；
3. 约束：与增强/治理同风格——失败计数进 enhance_stats（kind=dreaming），不阻断主流程；
4. 验收测试：种 3 组（近重复/同槽过期链/子串吸收 + RULE 豁免 + 曾命中豁免 + 跨 scope 豁免）→
   修剪正确、可恢复、journal 有痕；真实小说库 dry-run 报告冗余率（预期 <2% 才可上线自动档）。

## 五、与其它机制的关系

- 维度 D 治理（写入期 supersede）在前，Dreaming（离线复核+吸收）在后，双时间轴不变；
- 遗忘引擎（P4 衰减）单独走 retrieval_weight，不因 Dreaming 改变；
- RARE-lite 生成记忆桥（links_json）不参与冗余判定（桥是访问结构非事实）。