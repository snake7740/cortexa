"""场景 Schema（L3）：把场景语义声明为数据，而非代码。

设计文档：docs/design/scene-aware-architecture.md（v2.0）

承载（Review 修正后）：
- entity_types / lifecycle_states / event_predicates / relation_patterns
- transitions：事件→状态迁移表（death→EXITED, depart→DORMANT 等）
- context_required：哪些事件仅 NARRATION 语境可驱动迁移
- name_stopwords / dormant_threshold

Schema 是纯数据 + 校验；抽取算法在 L2（domain），内核（L1）不读 schema。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SceneSchema:
    """一个场景的全部领域语义（不可变，加载后只读）。"""

    name: str
    entity_types: tuple[str, ...]
    # 各实体类型的生命周期状态
    lifecycle_states: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 事件谓词：事件名 -> 触发词表
    event_predicates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 事件名 -> 允许的迁移（context_required 中的事件仅 NARRATION 触发）
    transitions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 需要 NARRATION 语境才可触发迁移的事件
    context_required: tuple[str, ...] = ()
    # 关系句式：命名组 (A,B) -> link_type
    relation_patterns: tuple[tuple[str, str], ...] = ()
    # 中文名过滤
    name_stopwords: frozenset = frozenset()
    # 已知实体主名单（Cast 种子：作者/图分析确认的角色）
    characters: tuple[str, ...] = ()
    # 别名映射：alias -> canonical（图分析实证：长生=叶长生/铁柱=赵铁柱 重复计数）
    aliases: dict[str, str] = field(default_factory=dict)
    # 出场空白多少章判定 DORMANT
    dormant_threshold_ch: int = 10
    # 场景来源（seed=预置 / 真实使用提炼）
    provenance: str = "seed"

    # -- 校验（Review 修正：加载即验，防 schema 与代码漂移）--
    def validate(self) -> None:
        errs: list[str] = []
        known_types = set(self.entity_types)
        for t, states in self.lifecycle_states.items():
            if t not in known_types:
                errs.append(f"lifecycle_states 引用了未声明实体类型 {t}")
        for ev, trans in self.transitions.items():
            if ev not in self.event_predicates:
                errs.append(f"transitions 引用了未声明事件 {ev}")
            to_state = trans.get("to")
            # 校验目标状态存在于某实体类型
            all_states = set()
            for states in self.lifecycle_states.values():
                all_states.update(states)
            if to_state not in all_states:
                errs.append(f"transitions[{ev}].to={to_state} 不在任何生命周期状态中")
            if ev in self.context_required and "from" not in trans:
                errs.append(f"context_required 事件 {ev} 缺 from 列表")
        for pat, link_type in self.relation_patterns:
            if "?P<A>" not in pat or "?P<B>" not in pat:
                errs.append(f"关系句式缺 A/B 命名组: {pat[:40]}")
        if errs:
            raise ValueError(f"SceneSchema[{self.name}] 校验失败:\n" + "\n".join(" - " + e for e in errs))


# ---------------------------------------------------------------------------
# Novel 场景（实证场景：由 208 章使用提炼）
# ---------------------------------------------------------------------------

NOVEL_SCHEMA = SceneSchema(
    name="novel",
    entity_types=("CHARACTER", "ITEM", "FACTION", "LOCATION", "STORY_THREAD"),
    lifecycle_states={
        "CHARACTER": ("INTRODUCED", "ACTIVE", "DORMANT", "EXITED", "REINTRODUCED", "TRANSFORMED"),
        "ITEM": ("ACQUIRED", "HELD", "TRANSFERRED", "DESTROYED"),
        "FACTION": ("FOUNDED", "ACTIVE", "DECLINED", "DISSOLVED"),
        "LOCATION": ("KNOWN", "ACTIVE", "SEALED", "DESTROYED"),
    },
    event_predicates={
        "death": ("死了", "身亡", "陨落", "身死", "被杀", "战死", "殒命", "毙命", "遇害", "惨死"),
        "depart": ("离开", "失踪", "下落不明", "出城", "远走", "不见踪影"),
        "return_": ("回来", "归来", "重逢", "现身", "回归"),
        "transform": ("突破", "晋升", "觉醒", "黑化", "入魔", "顿悟"),
    },
    transitions={
        # 事件 -> {from, to}：depart 只到 DORMANT（离场≠死亡）
        "death": {"from": ("INTRODUCED", "ACTIVE", "DORMANT"), "to": "EXITED"},
        "depart": {"from": ("ACTIVE",), "to": "DORMANT"},
        "return_": {"from": ("DORMANT",), "to": "REINTRODUCED"},
        "transform": {"from": ("INTRODUCED", "ACTIVE"), "to": "TRANSFORMED"},
    },
    context_required=("death",),  # 死亡仅 NARRATION 语境可驱动 EXITED
    relation_patterns=(
        # A拜B为师 / A是B的师父 / A与B结为道侣 / A与B是好友 / A是B的好友 / A是B的父母 / A与B结为仇敌
        (r"(?P<A>[^，。]{1,8}?)(?:拜|认|收)(?P<B>[^，。]{1,8}?)为师", "师徒"),
        (r"(?P<A>[^，。]{1,8}?)是(?P<B>[^，。]{1,8}?)的(?:师父|师尊|师傅)", "师徒"),
        (r"(?P<A>[^，。]{1,8}?)(?:与|和|同)(?P<B>[^，。]{1,8}?)结为道侣", "道侣"),
        (r"(?P<A>[^，。]{1,8}?)(?:与|和|同)(?P<B>[^，。]{1,8}?)是(?:多年)?(?:好友|朋友)", "好友"),
        (r"(?P<A>[^，。]{1,8}?)是(?P<B>[^，。]{1,8}?)的(?:多年)?(?:好友|朋友)", "好友"),
        (r"(?P<A>[^，。]{1,8}?)是(?P<B>[^，。]{1,8}?)的(?:亲生)?(?:父亲|母亲)", "亲子"),
        (r"(?P<A>[^，。]{1,8}?)(?:与|和|同)(?P<B>[^，。]{1,8}?)结为(?:生死)?仇(?:家|敌)", "仇敌"),
    ),
    name_stopwords=frozenset({
        "什么", "怎么", "为什么", "没有", "知道", "已经", "时候", "一个", "自己", "他们",
        "我们", "这个", "那个", "就是", "不是", "还有", "你们", "这里", "那里", "现在",
        "然后", "忽然", "但是", "不过", "身上", "手里", "眼里", "心里", "路上", "门外",
        "屋里", "城里", "天下", "人间", "眼前", "身后", "面前", "手中", "对方", "东西",
        "事情", "消息", "声音", "脚步", "目光", "眼神", "脸色", "口气", "样子", "地方",
        "方向", "时辰", "功夫", "本事", "法子",
    }),
    # 已知角色主名单（图分析实证的 208 章主要角色；完整 cast 表由真实使用迭代补充）
    characters=(
        "叶长生", "苏晚晴", "周元白", "恒远", "陈元吉", "韩执事", "方桐",
        "赵铁柱", "王宣", "沈渊", "沈阴", "孟知秋", "秦洛", "周暮", "宋远",
        "赵云飞", "林远", "面具长老", "持戒者", "老执事", "宗主", "灰衣人",
    ),
    # 别名消解（图分析实证的简写/切分碎片）——alias 指向 canonical
    aliases={
        "长生": "叶长生",
        "铁柱": "赵铁柱",
        "晚晴": "苏晚晴",
        "元白": "周元白",
    },
    dormant_threshold_ch=10,
    provenance="208章实证提炼",
)


# ---------------------------------------------------------------------------
# AML 比赛场景（S3：写入期治理维度 D）
# ---------------------------------------------------------------------------

AML_SCHEMA = SceneSchema(
    name="aml",
    entity_types=("USER", "ENTITY", "PREFERENCE", "RULE"),
    lifecycle_states={
        "USER": ("KNOWN", "ACTIVE", "UPDATED", "SUPERSEDED"),
        "ENTITY": ("KNOWN", "ACTIVE", "UPDATED", "SUPERSEDED"),
        "PREFERENCE": ("SEEN", "CURRENT", "CHANGED", "SUPERSEDED"),
        "RULE": ("ACTIVE", "SUPERSEDED", "WITHDRAWN"),
    },
    # 比赛治理事件（维度 D：该更新时更新/该遗忘时遗忘）
    event_predicates={
        "update": ("now", "currently", "changed", "修正", "现在", "改为"),
        "supersede": ("no longer", "不再", "取代", "替换", "prefer over"),
        "withdraw": ("don't want", "不再需要", "stop", "取消"),
    },
    transitions={
        # 新陈述覆盖旧陈述：CURRENT -> SUPERSEDED（旧偏好过期）
        "supersede": {"from": ("CURRENT", "ACTIVE", "UPDATED"), "to": "SUPERSEDED"},
        "update": {"from": ("KNOWN", "ACTIVE", "CURRENT", "UPDATED"), "to": "UPDATED"},
        "withdraw": {"from": ("CURRENT", "ACTIVE", "UPDATED"), "to": "SUPERSEDED"},
    },
    context_required=(),  # 比赛数据以平台陈述为主，语境要求宽松（治理是更新非死亡）
    # 比赛无显式中文句式（数据多为英文评测）——关系靠实体+偏好元数据
    relation_patterns=(),
    aliases={},
    provenance="AML 维度D 写入期治理设计",
)


# ---------------------------------------------------------------------------
# 注册表与加载
# ---------------------------------------------------------------------------

_SCENES: dict[str, SceneSchema] = {
    "novel": NOVEL_SCHEMA,
    "aml": AML_SCHEMA,
}


def get_schema(name: str = "novel") -> SceneSchema:
    """加载场景 schema（校验通过才返回）。"""
    if name not in _SCENES:
        raise ValueError(f"未知场景: {name}，可选: {sorted(_SCENES)}")
    schema = _SCENES[name]
    schema.validate()
    return schema


def register_schema(schema: SceneSchema) -> None:
    """注册新场景（S3 比赛/未来场景）。同名覆盖前校验。"""
    schema.validate()
    _SCENES[schema.name] = schema
