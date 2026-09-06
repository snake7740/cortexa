"""一致性审计（正式能力，本地零 LLM）：实体归属状态线 + 矛盾检测 + 时点视图。

业务定位：把"机械记忆"升级为"结构化记忆"的第一刀——
回答"谁、什么属性、什么值、哪一章"，支撑长篇内容（小说/代码文档/学术）
的一致性审计；同时是 AML 评测矛盾处理与时序维度的本地基线。

与原型实验（2026-08-31）相比的三条修正：
1. 实体归属：主语与境界词同句 + 状态动词 -> high 置信；同句无动词 -> low。
   只用 high 点判定矛盾，low 点仅在该章无 high 时兜底——消除原型
   "条目含主角名但修为说的是别人"造成的 24 处误报。
2. 宁可少报不可误报（precision-first 同哲学）：对话引号内独立境界句
   （"'炼气七层。'"）无主语归属，不报——漏检由调用方人工复核兜底。
3. 不物化 relations 表：全库扫描 <1s（1541 条实测），物化等规模化触发。

双闭环自身修复 #3（2026-08-31，audit 复跑暴露的三处取词歧义）：
- 同句多人清单误归属（Ch37"秦洛，炼气八层巅峰；周暮，炼气七层"取到七层）
  -> 分号段隔离：只处理含主语的分号段；
- 同段多等级词取错（Ch93"叶长生是炼气九层，他是炼气八层"取到八层）
  -> 从主语子句向后取第一个完整等级词；
- 跨子句归属假 high（Ch31"…瞥到灵力波动，这个老人至少有炼气九层的修为"）
  -> 等级词不在主语本子句时置信降 low（跨子句归属不可靠）；
  主语段无任何等级词时不记点（不再回退整句长词优先）。

双闭环自身修复 #4（2026-09-01，Ch1-176 全量复盘暴露的三类系统性偏差）：
- 状态线停在 Ch102：等级表无"巅峰/圆满"变体 -> 补 炼气九层巅峰 与 筑基/金丹/元婴圆满；
  带"巅峰"的词归一化为基阶（数值同基阶），"巅峰"仅用于命中，不改变数值语义；
- 误归属噪声（7 处）：境界词带修饰（以上/至少/左右/最多…）是模糊陈述非状态事实 -> 降 low；
  异常检测收窄为 high 点相邻比对（宁可少报不可误报同哲学），low 点仅保留在状态线供人工核；
- 物品假缺口（29 处）：代称/别名（戒=古虚戒）不算在场 -> items 支持"正名|别名"管道分隔。

设计边界（诚实声明）：
- 实体识别不做猜测：subjects 由调用方提供（作者知道主角名单）；
- 等级表 scale 是领域知识，默认提供修仙等级，可覆盖；
- 语义级冲突（叙事暗示 vs 设定矛盾）需要 LLM，属提分阶段，不在本模块。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

# 默认等级表（领域知识：中文网文修仙体系；调用方可整体覆盖）
DEFAULT_SCALE: dict[str, int] = {
    "炼气一层": 1, "炼气二层": 2, "炼气三层": 3, "炼气四层": 4, "炼气五层": 5,
    "炼气六层": 6, "炼气七层": 7, "炼气八层": 8, "炼气九层": 9,
    "炼气九层巅峰": 9,  # 归一化为九层（数值同基阶）；仅为最长匹配命中
    "筑基初期": 10, "筑基中期": 11, "筑基后期": 12, "筑基圆满": 12,
    "金丹初期": 13, "金丹中期": 14, "金丹后期": 15, "金丹圆满": 15,
    "元婴初期": 16, "元婴中期": 17, "元婴后期": 18, "元婴圆满": 18,
}

# 状态动词：出现则判定为"该句在陈述此实体的状态"（high 置信）
_STATE_VERB = re.compile(r"修为|境界|突破|晋升|晋为|升到|升为|到了|达到|已是|已经是|踏入|巅峰")
# 模糊修饰：境界词前后 2 字内出现 = 模糊陈述（至少/以上/左右…），非状态事实 -> 降 low。
# 复盘实证：Ch7"炼气四层以上"、Ch11"炼气五层左右"（后缀）；
# Ch9"至少炼气六层"、Ch40"最多炼气八层"（前缀）均为泛指/他人。
_HEDGE = re.compile(r"以上|至少|左右|上下|最多|不到|接近|约莫|怕是")

# 逻辑时间轴约定（novel_ingest.py 摄取与 slice_at 查询的唯一同源定义）：
# 设定层 = LOGICAL_T0；章节 N = LOGICAL_T0 + N * CHAPTER_INTERVAL_MS
LOGICAL_T0 = 1_760_000_000_000
CHAPTER_INTERVAL_MS = 3_600_000

_SENT_SPLIT = re.compile(r"[。！？!?\n]+")
_SEMI_SPLIT = re.compile(r"[；;]")  # 分号段：多人清单边界（"秦洛，八层；周暮，七层"）
_CLAUSE_SPLIT = re.compile(r"[，,：:]")  # 子句：同一主语的属性罗列边界
_CH_FROM_SESSION = re.compile(r"^ch(\d+)$")
_CH_FROM_CONTENT = re.compile(r"【第(\d+)章")


@dataclass(frozen=True)
class StatePoint:
    """单章状态点：subject 在该章的属性值。"""

    chapter: int
    value: str
    confidence: str  # high | low
    evidence: str    # 命中句（截断）
    mem_id: str


@dataclass(frozen=True)
class ConsistencyIssue:
    """一致性异常：regression 回跳 / jump 跳级 / item_gap 物品缺口。"""

    kind: str
    between: str  # 形如 "Ch12 -> Ch13"
    detail: str
    mem_id: str | None = None


@dataclass
class AuditReport:
    """审计报告：状态线 + 异常清单。as_dict() 供打印/落盘。"""

    scope: str
    subjects: list[str]
    state_lines: dict[str, list[StatePoint]] = field(default_factory=dict)
    issues: list[ConsistencyIssue] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "subjects": self.subjects,
            "state_lines": {
                s: [
                    {
                        "chapter": p.chapter,
                        "value": p.value,
                        "confidence": p.confidence,
                        "evidence": p.evidence,
                    }
                    for p in line
                ]
                for s, line in self.state_lines.items()
            },
            "issues": [
                {"kind": i.kind, "between": i.between, "detail": i.detail, "mem_id": i.mem_id}
                for i in self.issues
            ],
        }


def _chapter_of(session_id: str, content: str) -> int | None:
    """章号解析：session_id（chNNN 约定）优先，回退内容锚定前缀【第N章·】。"""
    m = _CH_FROM_SESSION.match(session_id or "")
    if m:
        return int(m.group(1))
    m = _CH_FROM_CONTENT.search(content or "")
    return int(m.group(1)) if m else None


def _find_realm(clause: str, realms_sorted: list[str]) -> tuple[str, bool] | None:
    """在子句内找最长等级词；返回 (词, 是否被模糊修饰)。

    修饰窗口取境界词前后各 2 字：后缀（"四层以上"）与前缀（"最多八层"）
    都属模糊陈述——泛指/估计，非该实体的状态事实。
    """
    for r in realms_sorted:
        idx = clause.find(r)
        if idx < 0:
            continue
        window = clause[max(0, idx - 2): idx] + clause[idx + len(r): idx + len(r) + 2]
        hedged = bool(_HEDGE.search(window))
        return r, hedged
    return None


def _state_points_in_memory(
    content: str, chapter: int, subject: str, scale: dict[str, int], mem_id: str
) -> list[StatePoint]:
    """句级扫描（同句多人消歧版）。

    - 分号段隔离多人清单：只处理含主语的段（他人属性不串段归属）；
    - 段内从主语子句起向后取第一个完整等级词（长词优先）；
    - 等级词在主语本子句 -> 置信按状态动词规则；在后续子句 -> 跨子句归属降 low；
    - 等级词被模糊修饰（以上/至少/左右…）-> 降 low（模糊陈述非状态事实）；
    - 段内无等级词 -> 不记点（precision-first，不回退整句长词优先）。
    """
    points: list[StatePoint] = []
    realms_sorted = sorted(scale, key=len, reverse=True)
    for sent in _SENT_SPLIT.split(content):
        if subject not in sent:
            continue
        for segment in _SEMI_SPLIT.split(sent):
            if subject not in segment:
                continue
            clauses = _CLAUSE_SPLIT.split(segment)
            hit: tuple[str, bool] | None = None
            cross_clause = False
            for i, clause in enumerate(clauses):
                if subject not in clause:
                    continue
                for j in range(i, len(clauses)):
                    found = _find_realm(clauses[j], realms_sorted)
                    if found is not None:
                        hit = found
                        cross_clause = j > i
                        break
                if hit is not None:
                    break
            if hit is None:
                continue
            realm, hedged = hit
            confidence = "low" if (cross_clause or hedged) else (
                "high" if _STATE_VERB.search(sent) else "low"
            )
            points.append(StatePoint(chapter, realm, confidence, sent[:60], mem_id))
            break  # 一句至多记一个点
    return points


def build_state_line(
    memories: list[tuple[str, str, str]], subject: str, scale: dict[str, int]
) -> list[StatePoint]:
    """构建单实体的章节状态线。

    memories: (mem_id, session_id, content) 元组列表（正文记忆）。
    每章聚合：high 点优先取等级最高者；无 high 时用 low 点兜底（置信标 low）。
    """
    by_chapter: dict[int, StatePoint] = {}
    for mem_id, session_id, content in memories:
        ch = _chapter_of(session_id, content)
        if ch is None:
            continue
        for pt in _state_points_in_memory(content, ch, subject, scale, mem_id):
            cur = by_chapter.get(ch)
            if cur is None:
                by_chapter[ch] = pt
                continue
            # 同章择优：high > low；同级置信取 scale 值更大者
            better = (
                pt.confidence == "high" and cur.confidence == "low"
            ) or (
                pt.confidence == cur.confidence
                and scale[pt.value] > scale[cur.value]
            )
            if better:
                by_chapter[ch] = pt
    return [by_chapter[ch] for ch in sorted(by_chapter)]


def check_state_line(
    line: list[StatePoint], scale: dict[str, int], jump_threshold: int = 3
) -> list[ConsistencyIssue]:
    """相邻状态点比对：值下降=回跳；跃升≥阈值=跳级。

    只在 high 置信点之间比对（复盘 #4 收窄）：low 点多为跨子句/修饰句归属，
    参与比对会把误归属放大为假异常；宁可少报不可误报，low 点保留在状态线供人工核。
    """
    issues: list[ConsistencyIssue] = []
    high_line = [p for p in line if p.confidence == "high"]
    for prev, cur in zip(high_line, high_line[1:]):
        pv, cv = scale[prev.value], scale[cur.value]
        between = f"Ch{prev.chapter} -> Ch{cur.chapter}"
        if cv < pv:
            issues.append(
                ConsistencyIssue(
                    "regression",
                    between,
                    f"状态从「{prev.value}」回到「{cur.value}」（回跳需人工核对：真矛盾或设定例外）",
                    cur.mem_id,
                )
            )
        elif cv - pv >= jump_threshold:
            issues.append(
                ConsistencyIssue(
                    "jump",
                    between,
                    f"状态从「{prev.value}」跳到「{cur.value}」（+{cv - pv} 级，可能为快速突破设定，需人工核对）",
                    cur.mem_id,
                )
            )
    return issues


def check_item_gap(
    memories: list[tuple[str, str, str]],
    item: str,
    min_gap: int = 3,
    aliases: tuple[str, ...] = (),
) -> list[ConsistencyIssue]:
    """物品连续性：出现章集合中间隔 ≥ min_gap 报缺口（消失多章后复现）。

    aliases：同一物品的代称/别名（如"古虚戒"之"戒"）。在场判定为任一名字命中——
    别名只会放宽在场、不会凭空造缺口（复盘 #4：字面口径造成 29 处假 item_gap）。
    """
    names = (item, *aliases)
    chapters = sorted(
        {
            ch
            for mem_id, session_id, content in memories
            if any(name in content for name in names)
            and (ch := _chapter_of(session_id, content)) is not None
        }
    )
    issues: list[ConsistencyIssue] = []
    for a, b in zip(chapters, chapters[1:]):
        if b - a >= min_gap:
            issues.append(
                ConsistencyIssue(
                    "item_gap",
                    f"Ch{a} -> Ch{b}",
                    f"「{item}」缺席 Ch{a + 1}-Ch{b - 1}（{b - a - 1} 章）后复现",
                )
            )
    return issues


def slice_at(
    conn: sqlite3.Connection, scope: str, chapter: int, *, scale: dict[str, int], subject: str
) -> list[StatePoint]:
    """时点视图：≤指定章节的记忆切片中，subject 的最近状态（双时间轴结构性能力）。

    不设 LIMIT：排序发生在内存（confidence+章号）且只取 top3，固定行限额
    在长篇规模下会把早章状态点静默丢弃（1737 条 review 实证后移除）。
    """
    cutoff = LOGICAL_T0 + chapter * CHAPTER_INTERVAL_MS  # 同源逻辑时间轴
    rows = conn.execute(
        "SELECT id, session_id, content FROM memories "
        "WHERE scope = ? AND status = 'ACTIVE' AND event_ts_ms <= ? "
        "ORDER BY event_ts_ms DESC",
        (scope, cutoff),
    ).fetchall()
    points: list[StatePoint] = []
    for mem_id, session_id, content in rows:
        ch = _chapter_of(session_id, content)
        if ch is None:
            continue
        points.extend(_state_points_in_memory(content, ch, subject, scale, mem_id))
    # 高置信优先，取时点内最近的等级
    points.sort(key=lambda p: (p.confidence != "high", -p.chapter))
    return points[:3]


def audit(
    conn: sqlite3.Connection,
    scope: str,
    *,
    subjects: list[str],
    items: list[str] | None = None,
    scale: dict[str, int] | None = None,
) -> AuditReport:
    """一致性审计总入口：状态线 + 矛盾 + 物品缺口（全库只读扫描，零 LLM）。

    items 条目支持"正名|别名1|别名2"管道分隔（代称在场判定，见 check_item_gap）。
    """
    scale = scale or DEFAULT_SCALE
    rows = conn.execute(
        "SELECT id, session_id, content FROM memories "
        "WHERE scope = ? AND status = 'ACTIVE' "
        "AND (session_id LIKE 'ch%' OR content LIKE '【第%章%')",
        (scope,),
    ).fetchall()
    report = AuditReport(scope=scope, subjects=list(subjects))
    memories = [(r["id"], r["session_id"], r["content"]) for r in rows]
    for subject in subjects:
        line = build_state_line(memories, subject, scale)
        report.state_lines[subject] = line
        report.issues.extend(check_state_line(line, scale))
    for spec in items or []:
        primary, *aliases = spec.split("|")
        report.issues.extend(
            check_item_gap(memories, primary, aliases=tuple(aliases))
        )
    return report
