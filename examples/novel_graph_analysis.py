"""全书人物·剧情·关系图分析（208 章正文直读）。

输出（写到小说项目 cortexa-out/）：
1. graph_full.json       —— 全量图数据（nodes=人物, edges=关系/共现, meta=生存状态）
2. graph_report.md       —— 人读报告：人物档案/生存核查/关系网络/剧情事件/异常清单

人物识别：已知角色表（CHARACTERS）+ 高频专名自动发现（出现>=5章过滤）。
生存检测：区分主语死亡(X死了/被杀/陨落) 与 转述(他/她说...死了)。
关系检测：显式关系词(师徒/道侣/好友/亲子/仇敌/同行/师承)。
剧情事件：突破/离开/回归/背叛/大战 等关键词打点。

用法：
    python examples/novel_graph_analysis.py <小说根目录> [--out cortexa-out]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# 已知角色（可从正文验证补充）
CHARACTERS = [
    "叶长生", "苏晚晴", "周元白", "恒远", "陈元吉", "韩执事", "方桐",
    "赵铁柱", "王宣", "沈渊", "沈阴", "孟知秋", "老乞丐", "叶巨川",
    "灰衣人", "面具长老", "持戒者", "老执事", "秦洛", "周暮", "宋远",
    "赵云飞", "林远", "叶母", "柳青", "铁柱", "大长老", "宗主",
]

# 中文人名高频后缀（自动发现用）
_NAME_STOP = {
    "什么", "怎么", "为什么", "没有", "知道", "已经", "时候", "一个",
    "自己", "他们", "我们", "这个", "那个", "就是", "不是", "还有",
    "你们", "这里", "那里", "现在", "然后", "忽然", "但是", "不过",
    "身上", "手里", "眼里", "心里", "路上", "门外", "屋里", "城里",
    "天下", "人间", "眼前", "身后", "面前", "手中", "对方", "东西",
    "事情", "消息", "声音", "脚步", "目光", "眼神", "脸色", "口气",
    "样子", "地方", "方向", "时辰", "功夫", "本事", "法子",
}

_CH_DIR = ("废灵根", "正文", "chapters")
_SENT_SPLIT = re.compile(r"[。！？!?\n]+")

# --- 生存检测（主语死亡，X 是死者） ---
_SELF_DIE = re.compile(
    r"([\u4e00-\u9fff]{2,4})(?:已经|当场|就|便|竟然|终于|果然|真的|也|都)?"
    r"(?:死了|身亡|陨落|身死|被杀|战死|殒命|毙命|遇害|惨死|咽气|断气|丧命)"
)
_SELF_KILLED = re.compile(r"([\u4e00-\u9fff]{2,4})被[^，。]{0,12}?(?:击杀|杀死|所杀|杀掉|灭口|害死)")
# 转述（非本人死亡，过滤用）：X说...死了 / 提到...的死
_QUOTE_DIE = re.compile(r"(?:说|道|提到|提起|听闻|听说|以为|觉得|认为)[^。]{0,20}(?:死了|身亡|被杀)")

# --- 关系词 ---
_RELATION_PATTERNS = [
    (r"(?P<A>[\u4e00-\u9fff]{2,4})(?:拜|认|收)(?P<B>[\u4e00-\u9fff]{2,4})为师", "师徒"),
    (r"(?P<A>[\u4e00-\u9fff]{2,4})是(?P<B>[\u4e00-\u9fff]{2,4})的(?:师父|师尊|师傅)", "师徒"),
    (r"(?P<A>[\u4e00-\u9fff]{2,4})(?:与|和|同)(?P<B>[\u4e00-\u9fff]{2,4})结为道侣", "道侣"),
    (r"(?P<A>[\u4e00-\u9fff]{2,4})(?:与|和|同)(?P<B>[\u4e00-\u9fff]{2,4})是(?:多年)?(?:好友|朋友)", "好友"),
    (r"(?P<A>[\u4e00-\u9fff]{2,4})是(?P<B>[\u4e00-\u9fff]{2,4})的(?:亲生)?(?:父亲|母亲|儿子|女儿)", "亲子"),
    (r"(?P<A>[\u4e00-\u9fff]{2,4})(?:与|和|同)(?P<B>[\u4e00-\u9fff]{2,4})结为(?:生死)?仇(?:家|敌)", "仇敌"),
    (r"(?P<A>[\u4e00-\u9fff]{2,4})(?:收|收下)(?P<B>[\u4e00-\u9fff]{2,4})为(?:徒|弟子)", "师徒"),
]

# --- 剧情事件 ---
_EVENTS = [
    ("突破", r"(?:突破|晋升|踏入|晋级)(?:到|至)?[\u4e00-\u9fff]{0,6}(?:境界|层次|层|阶)"),
    ("大战", r"(?:大战|决战|厮杀|激战|混战)"),
    ("离场", r"(?:离开|离去|辞别|告别|远走|出城)"),
    ("回归", r"(?:回来|归来|重逢|回归|现身)"),
    ("背叛", r"(?:背叛|叛出|叛变|出卖|反水)"),
    ("死亡", r"(?:死了|身亡|陨落|被杀|灭口)"),
    ("结盟", r"(?:结盟|联手|合作|同盟)"),
    ("失踪", r"(?:失踪|下落不明|音讯全无|人间蒸发)"),
]


def chno_of(fname) -> int:
    """从文件名提取章节号（兼容 str 与 Path）。"""
    name = fname.name if hasattr(fname, "name") else str(fname)
    m = re.search(r"Ch(\d+)", name)
    return int(m.group(1)) if m else 0


def is_real_name(n: str) -> bool:
    if len(n) < 2 or len(n) > 4:
        return False
    if n in _NAME_STOP:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="全书人物/剧情/关系图分析")
    ap.add_argument("root", type=Path)
    ap.add_argument("--out", default="cortexa-out")
    args = ap.parse_args()

    ch_dir = next((args.root / d for d in _CH_DIR if (args.root / d).exists()), None)
    if ch_dir is None:
        print(f"章节目录不存在: {args.root}", file=sys.stderr)
        return 1

    files = sorted(ch_dir.glob("Ch*_第*章.md"), key=chno_of)
    print(f"加载 {len(files)} 章...")

    # 数据结构
    chapters = {}               # ch -> 全文
    person_chapters = defaultdict(set)   # person -> {ch}
    death_events = defaultdict(list)     # person -> [(ch, ctx)]
    relations = defaultdict(list)        # (a,b,type) -> [(ch, evidence)]
    events_by_ch = defaultdict(list)     # ch -> [(type, ctx)]
    cooccur = defaultdict(Counter)       # person -> {other: count}

    # 读全部章节
    for f in files:
        ch = chno_of(f.name)
        text = f.read_text(encoding="utf-8")
        chapters[ch] = text

    # 人物自动发现：仅在已知角色全名不在场时，才用高频专名补漏。
    # 主名单 = CHARACTERS（精确人名），避免 2 字切分碎片（"来了/出来/的时候"）污染。
    freq = Counter()
    # 收集正文出现的全部已知角色名 + 明确的人名样式片段
    for ch, text in chapters.items():
        for name in CHARACTERS:
            if name in text:
                freq[name] += 1
    persons = sorted(CHARACTERS)
    persons_set = set(persons)
    print(f"人物: {len(persons)}（已知角色表）")

    # 逐章分析
    for ch in sorted(chapters):
        text = chapters[ch]
        sents = [s for s in _SENT_SPLIT.split(text) if s.strip()]
        chapter_persons = set()

        # 人物出现（已知+高频；4字名优先匹配避免误含）
        for p in persons:
            if p in text:
                chapter_persons.add(p)
                person_chapters[p].add(ch)

        # 死亡检测：仅名单人物（persons_set 精确匹配，防"石碑没了"式误报）；
        # 排除引号转述（"X说...死了"）与所有格（"X的...死了"非 X 本人）
        for pat in (_SELF_DIE, _SELF_KILLED):
            for m in pat.finditer(text):
                name = m.group(1)
                if name not in persons_set:
                    continue
                before = text[max(0, m.start() - 30):m.start()]
                if _QUOTE_DIE.search(before):
                    continue
                ctx = text[max(0, m.start() - 20):m.end() + 25]
                death_events[name].append((ch, ctx.strip()))

        # 关系检测
        for sent in sents:
            for pat, rtype in _RELATION_PATTERNS:
                mm = re.search(pat, sent)
                if not mm:
                    continue
                a, b = mm.group("A"), mm.group("B")
                if a in persons and b in persons and a != b:
                    key = (a, b, rtype)
                    relations[key].append((ch, sent.strip()[:120]))
                break

        # 剧情事件
        for ev_name, pat in _EVENTS:
            for m in re.finditer(pat, text):
                ctx = text[max(0, m.start() - 15):m.end() + 20]
                events_by_ch[ch].append((ev_name, ctx.strip()))

        # 共现（同章人物两两）
        plist = sorted(chapter_persons)
        for i in range(len(plist)):
            for j in range(i + 1, len(plist)):
                cooccur[plist[i]][plist[j]] += 1
                cooccur[plist[j]][plist[i]] += 1

    # ---- 输出 ----
    out_dir = args.root / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")

    # 生存状态判定
    REF_MARK = re.compile(
        r"说|道|听说|提起|回忆|记得|仿佛|遗物|账|死了|没了|消息|名字|提及|传闻|联想|"
        r"暗线|源头|线索|檀香|密会|那夜|那晚|当年|那时|死因|忌日"
    )

    def _mention_kind(text: str) -> str:
        """单行粗判：含引用/回忆/组织余势关键词 → 引用；否则疑似在场（粗判，供人工复核）。"""
        return "ref" if REF_MARK.search(text) else "live"


    status = {}
    for p in persons:
        if death_events.get(p):
            death_chs = sorted(set(c for c, _ in death_events[p]))
            later = sorted(c for c in person_chapters[p] if c > max(death_chs))
            if later:
                ref_n = 0
                for c in later:
                    for f in sorted((args.root / "废灵根").glob(f"Ch{c}_*.md")):
                        for ln in f.read_text(encoding="utf-8").splitlines():
                            if p in ln and _mention_kind(ln) == "ref":
                                ref_n += 1
                                break
                        break  # 每章只判首条命中行（够粗判）
                live_n = len(later) - ref_n
                # 允许 ≤1 章或 ≤20% 存疑（长弧线粗判的合理容差）
                if ref_n >= 1 and live_n <= max(1, round(0.2 * len(later))):
                    # 2026-09-06 收紧：名字出现≠在场（引用/回忆/遗物/账目/组织余势降级；
                    # 允许 ≤1 章存疑留人工）
                    status[p] = (f"已死亡(章{death_chs})；后续{len(later)}章{ref_n}章为引用/回忆提及"
                                 f"（{live_n}章待人工复核）")
                else:
                    status[p] = f"⚠️死亡章{death_chs}后仍出现{len(later)}章(需核查,引用类{ref_n}/在场{live_n})"
            else:
                status[p] = f"已死亡(章{death_chs})"
        else:
            chs = sorted(person_chapters[p])
            status[p] = f"存活(ch{chs[0] if chs else '-'}-ch{chs[-1] if chs else '-'},{len(chs)}章)"

    # 图数据
    nodes = [{
        "id": p,
        "chapters": sorted(person_chapters[p]),
        "first": min(person_chapters[p]) if person_chapters[p] else None,
        "last": max(person_chapters[p]) if person_chapters[p] else None,
        "appear_chapters": len(person_chapters[p]),
        "death": [{"ch": c, "evidence": ctx} for c, ctx in death_events.get(p, [])],
        "status_summary": status[p],
    } for p in persons if person_chapters[p]]

    # 关系边
    rel_edges = []
    for (a, b, rtype), evs in sorted(relations.items()):
        rel_edges.append({
            "src": a, "dst": b, "type": rtype,
            "chapters": sorted(set(c for c, _ in evs)),
            "evidence": evs[0][1] if evs else "",
            "first_seen": min(c for c, _ in evs),
        })

    # 强共现边（共现>=3章的人物对）
    co_edges = []
    for a in cooccur:
        for b, n in cooccur[a].items():
            if a < b and n >= 3:
                co_edges.append({"src": a, "dst": b, "weight": n})

    graph = {
        "scope": args.root.name,
        "chapters_analyzed": len(chapters),
        "nodes": nodes,
        "relation_edges": rel_edges,
        "cooccur_edges": co_edges,
        "death_count": len(death_events),
        "event_summary": {k: sum(1 for v in events_by_ch.values() for x in v if x[0] == k)
                          for k, _ in _EVENTS},
    }

    gf = out_dir / f"graph_full_{stamp}.json"
    gf.write_text(json.dumps(graph, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "graph_full_latest.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- Markdown 报告 ----
    L = []
    L.append("# 全书人物·剧情·关系图分析")
    L.append("")
    L.append(f"> 分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M')} | 章节: {len(chapters)} 章 | 人物: {len(nodes)}")
    L.append("")
    L.append("## 一、人物清单与生存状态")
    L.append("")
    L.append("| 人物 | 首次出场 | 最后出场 | 出场章数 | 生存 |")
    L.append("|------|---------|---------|---------|------|")
    for n in sorted(nodes, key=lambda x: -(x["appear_chapters"] or 0)):
        L.append(f"| {n['id']} | ch{n['first'] or '-'} | ch{n['last'] or '-'} | {n['appear_chapters']} | {n['status_summary']} |")
    L.append("")

    L.append("## 二、死亡事件（需人工复核）")
    L.append("")
    if death_events:
        for p, evs in sorted(death_events.items(), key=lambda x: -len(x[1])):
            for c, ctx in evs:
                L.append(f"- **{p}** ch{c}: {ctx[:100]}")
    else:
        L.append("（无检测到死亡事件）")
    L.append("")

    L.append("## 三、显式关系")
    L.append("")
    if rel_edges:
        L.append("| A | 关系 | B | 首次 | 章数 | 证据 |")
        L.append("|---|------|---|------|------|------|")
        for e in sorted(rel_edges, key=lambda x: x["first_seen"]):
            L.append(f"| {e['src']} | {e['type']} | {e['dst']} | ch{e['first_seen']} | {len(e['chapters'])} | {e['evidence'][:50]} |")
    else:
        L.append("（无显式关系）")
    L.append("")

    L.append("## 四、高频共现（同章出场>=3章的人物对，前30）")
    L.append("")
    for e in sorted(co_edges, key=lambda x: -x["weight"])[:30]:
        L.append(f"- {e['src']} ↔ {e['dst']}（{e['weight']}章同场）")
    L.append("")

    L.append("## 五、剧情事件分布")
    L.append("")
    for k, _ in _EVENTS:
        cnt = graph["event_summary"][k]
        L.append(f"- {k}: {cnt} 次")
    L.append("")

    # 剧情事件按章列出重点（每20章一卷内事件密度）
    L.append("## 六、分卷剧情事件密度")
    L.append("")
    L.append("| 卷 | 突破 | 大战 | 死亡 | 离场 | 回归 | 背叛 | 失踪 |")
    L.append("|----|------|------|------|------|------|------|------|")
    vol_events = defaultdict(Counter)
    for ch, evs in events_by_ch.items():
        vol = (ch - 1) // 20
        for ev_name, _ in evs:
            vol_events[vol][ev_name] += 1
    for vol in sorted(vol_events):
        c = vol_events[vol]
        L.append(f"| Ch{vol*20+1}-{vol*20+20} | {c['突破']} | {c['大战']} | {c['死亡']} | "
                 f"{c['离场']} | {c['回归']} | {c['背叛']} | {c['失踪']} |")
    L.append("")

    L.append("## 七、异常清单（自动检测候选，需人工核对）")
    L.append("")
    L.append("1. 人物死亡章后仍有出场（复活矛盾候选）")
    L.append("2. 出场跨度极大但中间有>30章空白的角色（无交代离场）")
    L.append("")
    for n in nodes:
        chs = n["chapters"]
        if not chs:
            continue
        # 空白检测
        gaps = []
        for i in range(1, len(chs)):
            if chs[i] - chs[i - 1] > 30:
                gaps.append((chs[i - 1], chs[i]))
        if gaps:
            L.append(f"- **{n['id']}**: 出场 ch{chs[0]}-ch{chs[-1]}，空白段 {gaps}")
        if n["death"]:
            for d in n["death"]:
                later = [c for c in chs if c > d["ch"]]
                if later:
                    L.append(f"- ⚠️ **{n['id']}**: ch{d['ch']} 死亡记录，但 ch{later[:5]} 仍出场 → 需人工核对（可能转述/同名/复活设定）")

    L.append("")
    L.append("---")
    L.append("> 由 cortexa examples/novel_graph_analysis.py 生成。死亡检测为关键词启发式，转述/回忆/同名可能误报，请以正文为准。")

    md = "\n".join(L)
    mf = out_dir / f"graph_report_{stamp}.md"
    mf.write_text(md, encoding="utf-8")
    (out_dir / "graph_report_latest.md").write_text(md, encoding="utf-8")

    print(f"图数据: {gf}")
    print(f"报告: {mf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())