# 三主体集成地图（复盘单入口）

> 更新：2026-08-31；2026-09-06 批②刷新（正文 208 章、记忆库 2620 条、报告单点归位、webnovel state 处置=B 渐进接管）。本文件是三个主体协同工作的总契约：**谁在哪、产出在哪、命令怎么跑、复盘从哪开始**。
> 三主体解耦原则：互不修改对方代码与数据，只通过下表的文件位置协作；任一主体可独立替换。

## 一、三主体职责与边界

| 主体 | 位置 | 职责 | 自留地（独占读写） |
| --- | --- | --- | --- |
| **webnovel-writer**（创作流） | `C:\webnovel-writer-master\webnovel-writer-master\`（Claude Code 插件，零改动） | 写作工作流：`/webnovel-plan` `/webnovel-write` `/webnovel-review`；防幻觉三定律（大纲即法律/设定即物理/发明需识别） | `.webnovel/`（state.json / index.db / summaries / backups） |
| **小说项目**（内容资产） | 本目录 | 正文、设定集、大纲的唯一权威 | `废灵根/`（正文，**当前 208 章**）、`设定集/`、`大纲/` |
| **Cortexa**（记忆系统） | `C:\Cortexa\cortexa-mvp\` | 记忆库与审计：摄取/检索/一致性审计/任务流水 | `.cortexa/`（记忆库）、`cortexa-out/`（全部产出） |

## 二、创作环路（记忆有效性验证闭环）

```
/webnovel-write 新章 ──► 废灵根/ChNNN_标题.md          [创作流产出内容]
        │
        ▼
bridge sync ─────────► .cortexa/ 记忆库增量摄取        [输入：幂等，stored=0 即严格一致]
        │
        ▼
写前 recall / 设定查询 ──（facade.recall / novel_probe）[输出：记忆反哺创作，写前防幻觉]
        │
        ▼
bridge audit ────────► 状态线/矛盾/物品缺口            [记忆混乱度量化：1万/5万/10万字不乱]
        │
        ▼
cortexa-out/ 报告归档 ──► status / sync / audit 报告    [单点复盘]
        │
        └──────────► 反哺下一章写作决策（修矛盾、补伏笔）
```

**记忆健康度四指标**（环路上每个环节可量化）：

1. **sync 幂等差**：重跑新增条数，0 = 记忆库与正文严格一致；
2. **审计异常数**：regression（回跳）/ jump（跳级）/ item_gap（物品缺口），随字数增长不失控；
3. **探针命中率**：`novel_probe.py` 8 题事实检索；
4. **孤岛率**：`status` 中检索覆盖（未被问过 ≠ 检索不到）。

## 三、目录契约（产出归位表）

| 位置 | 归属 | 内容 |
| --- | --- | --- |
| `废灵根/Ch*.md` | 内容资产 | 正文章节（**唯一权威**，当前 208 章；Ch108-208 Cortexa 闭环产出，loop_check 全量 101 份） |
| `设定集/`、`大纲/` | 内容资产 | 世界观/角色/力量体系/总纲/爽点规划 |
| `.webnovel/` | webnovel-writer | state.json（进度/主角状态/伏笔 28 条/活跃线 12 条）、index.db、summaries |
| `.cortexa/` | Cortexa | 记忆库 cortexa.db（**2620 条 ACTIVE**，scope=本目录名；含 7 link_type + 8 条实体边；2026-09-06 修正 41 条 CODE 误标→FACT 后类型分层归位；`novel_links.py` 幂等回填） |
| `cortexa-out/` | **Cortexa 产出归位**（单点归档契约） | status_latest.md（健康度快照）、sync_*.md（同步报告）、audit_*.md（审计报告）、loop_check_*.md（环路检查 Ch108-208）、next_context_Ch*.md（写前上下文包）、故事方向_*.md（弧线规划）、relocate_report_*.md（2026-09-06 归位记录）、Cortexa记忆脉络报告.md |
| `.claude/.webnovel-current-project` | 指针 | 内容=本目录绝对路径；`/webnovel-*` 免传参定位本书 |
| `webnovel-platform/` | 用户方法论 | 五段式创作平台模板（00_配置→04_审核），独立于上述三主体 |

## 四、命令手册

```powershell
# 前置：Cortexa 已 pip install -e 安装（显式 Python：C:\Programs\Python\Python312\python.exe）
$PY = "C:\Programs\Python\Python312\python.exe"
$BOOK = "C:\Users\junzh\新建文件夹\废灵根修仙-重构版"

# 1) 三主体健康度快照（复盘第一步）
& $PY C:\Cortexa\cortexa-mvp\examples\novel_bridge.py $BOOK status

# 2) 写完新章后：增量回灌记忆库（幂等；报告落 cortexa-out/）
& $PY C:\Cortexa\cortexa-mvp\examples\novel_bridge.py $BOOK sync

# 3) 一致性审计（报告落 cortexa-out/；实体/物品可覆盖）
& $PY C:\Cortexa\cortexa-mvp\examples\novel_bridge.py $BOOK audit --subject 叶长生 --item 黑色木牌

# 4) 事实检索探针（8 题量化命中率）
& $PY C:\Cortexa\cortexa-mvp\examples\novel_probe.py $BOOK

# 5) webnovel-writer CLI（不进 Claude Code 也可用；指针已写，Claude 内 /webnovel-* 直接可用）
& $PY C:\webnovel-writer-master\webnovel-writer-master\webnovel-writer\scripts\webnovel.py --project-root $BOOK status -- --focus all
```

## 五、历史遗留登记（只登记，未擅动）

| 项 | 事实 | 处置建议 |
| --- | --- | --- |
| 根目录 106 个章节 md | 经 MD5 逐字节对比，与 `废灵根/` 完全相同（旧拷贝） | 确认后可清理；当前不参与摄取，无风险 |
| 根目录 8 个 Ch*.md 报告 | `Ch31-40_执行方案.md`、`Chain-B_*检查报告.md` 等，是报告非章节 | 属既有报告资产，建议移入既有报告目录 |
| webnovel state 落后（2026-09-06 处置中） | state.json 曾停"第 105 章完成"（2026-04-23）；正文实际已至 Ch208（废灵根/ 208 章） | 处置方案 = B 渐进接管（Cortexa novel_loop place --sync-state 自动追平 progress 字段；富字段标 stale_since 不伪造）。正文续写走 Cortexa 闭环，webnovel-writer 保留目录契约 |
| 审计"同句多人"局限 | 如 Ch37"秦洛，炼气八层巅峰；周暮，炼气七层"同句时取词可能歧义，产生 high/high 回跳候选 | precision-first 设计：报出供人工核对；语义级消歧属 LLM 提分阶段 |

## 六、复盘路径（30 秒版）

1. 打开 `cortexa-out/status_latest.md` → 三主体状态一览（2026-09-06 起根目录散落报告已统一归位，单入口成立）；
2. 看记忆混乱度行 → 异常类型与数量，需要详情跑 `audit`；
3. 看 sync 最新报告 → 记忆库与正文是否一致；
4. 写新章前 → `recall` 查前情设定（或读 `.webnovel/state.json` 的 next_chapter_hints）；
5. 写完 → `sync` → `audit` → 闭环。
