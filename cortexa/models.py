"""Cortexa 核心数据模型与 AML 官方契约模型。

术语对照（全项目唯一叫法，见 README 术语表）：
- MemoryInstance   记忆实例（原子记忆，Instance 层）
- Wing/Room/Cluster 知识拓扑地图三层（业务文档 8.2），MVP 仅落 wing/room 字段
- FSRSState        巩固调度状态（业务文档 8.1，DSR 简化版）
- scope            官方契约检索隔离键（原样存储、Search 时完全一致）

AML 契约模型（AddRequest/SearchResponse 等）按官方 api-guide 语义还原，
使用 AliasChoices 做宽松别名解析。2026-08-31 核实：官方 refs/aml-contracts
六个样例管线只直调 /chat/completions（答题/评审），不直调记忆 API——
字段精确对齐须等 9/20 官方 Smoke 回执，在各 AML_CONTRACT_ALIGNMENT 标注处调整。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from enum import Enum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 领域枚举
# ---------------------------------------------------------------------------

class MemoryType(str, Enum):
    """记忆类型。RULE 独立成 Wing 且永不衰减（业务文档 8.3，AML 维度 F）。"""

    FACT = "FACT"
    RULE = "RULE"
    PREFERENCE = "PREFERENCE"
    EVENT = "EVENT"
    PROCEDURE = "PROCEDURE"
    CODE_DEBUG = "CODE_DEBUG"
    CODE_DEV = "CODE_DEV"


class MemoryStatus(str, Enum):
    """记忆生命周期状态（双时间轴 supersede 语义，业务文档 8.5）。"""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"  # 被新记忆取代（保留可追溯，维度 C）
    ARCHIVED = "ARCHIVED"  # 整合吸收后归档（业务文档 8.5 整合吸收）
    FORGOTTEN = "FORGOTTEN"  # 衰减遗忘


# ---------------------------------------------------------------------------
# 巩固调度状态（业务文档 8.1：FSRS DSR 模型简化版）
# ---------------------------------------------------------------------------

class FSRSState(BaseModel):
    """R(t) = (1 + t/(9*S))^(-1)（业务文档 8.1 遗忘曲线公式）。"""

    stability: float = 1.0  # S：记忆强度（天）
    difficulty: float = 5.0  # D：难度 0-10，被纠正时上调
    consolidation_level: int = 0  # 巩固等级，达到 core 阈值进入核心记忆区
    last_review_at: float | None = None  # 最近一次成功检索（epoch 秒）

    def retrievability(self, now: float | None = None) -> float:
        """按遗忘曲线计算当前可提取概率。"""
        if self.last_review_at is None:
            return 0.0
        now = now if now is not None else time.time()
        elapsed_days = max((now - self.last_review_at) / 86400.0, 0.0)
        return (1.0 + elapsed_days / (9.0 * max(self.stability, 1e-6))) ** -1


def new_id() -> str:
    return uuid.uuid4().hex


def content_hash(scope: str, content: str) -> str:
    """SHA-256 去重指纹：scope 内去重（业务文档 7.1 L2 感官过滤）。"""
    return hashlib.sha256(f"{scope}\x00{content}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# 核心记忆实例
# ---------------------------------------------------------------------------

class MemoryInstance(BaseModel):
    """原子记忆。原文 content 永不丢弃（维度 A 防语义漂移）。"""

    id: str = Field(default_factory=new_id)
    scope: str  # 官方契约：检索隔离键
    session_id: str | None = None  # 官方契约：仅用于分组，不做隔离
    role: str | None = None  # 官方契约消息角色（user/assistant）
    content: str

    mem_type: MemoryType = MemoryType.FACT
    status: MemoryStatus = MemoryStatus.ACTIVE
    content_hash: str = ""

    event_ts_ms: int | None = None  # 官方契约：消息时间戳（Unix 毫秒，可用时）
    created_ts_ms: int = Field(default_factory=lambda: int(time.time() * 1000))

    # 双时间轴（业务文档 12.x ADR-0004：维度 C 从第一天进模型）
    valid_from_ms: int | None = None
    valid_to_ms: int | None = None
    superseded_by: str | None = None

    # 知识拓扑定位（业务文档 8.2；MVP 落 wing/room，Cluster P2 启用）
    wing: str = "general"
    room: str = ""

    # L1 感知输出（维度 E 个性化编码）
    entities: list[str] = Field(default_factory=list)
    topic: str = ""
    intent: str = ""
    confidence: float = 0.5

    # 治理属性
    no_decay: bool = False  # RULE/核心记忆永不衰减（维度 F 红线）
    retrieval_weight: float = 1.0  # 衰减遗忘作用于该权重（业务文档 8.5）
    fsrs: FSRSState = Field(default_factory=FSRSState)
    links: list[str] = Field(default_factory=list)  # 簇间桥梁（维度 B，P2）

    source_request_id: str | None = None  # 官方 Add 请求 ID 追溯

    # 写入类型化（Palantir Action type 移植，2026-08-31）：区分首次写入与修正写入；
    # retcon 必须携带 supersedes_id 形成强制取代链（编辑史可追溯，同官方 edits history）
    op_type: str = "add"  # add | retcon
    supersedes_id: str | None = None  # retcon 时被修正的旧记忆 id

    def active_dimensions(self) -> set[str]:
        """当前实例可观测到的结构维度集合（维度地板审计输入）。"""
        dims: set[str] = set()
        if self.content.strip():
            dims.add("CONTENT")
        if self.entities:
            dims.add("ENTITIES")
        if self.topic:
            dims.add("TOPIC")
        if self.intent:
            dims.add("INTENT")
        if self.confidence > 0:
            dims.add("CONFIDENCE")
        if self.mem_type == MemoryType.RULE:
            dims.add("RULE")
            dims.add("PROCEDURAL")  # 规则蕴含程序性约束（维度地板：RULE wing 必含）
        if self.mem_type == MemoryType.PROCEDURE:
            dims.add("PROCEDURAL")
        if self.mem_type == MemoryType.PREFERENCE:
            dims.add("PREFERENCE")
        if self.event_ts_ms is not None or self.valid_from_ms is not None:
            dims.add("TEMPORAL")
        return dims


# ---------------------------------------------------------------------------
# AML 官方契约模型（2026-09-06 对照仓库根 _ref_apiguide.html 现行同步规范对齐，
# golden 样例固化为 tests/golden/official_aml_contract.json）
#   官方 Add 请求   : request_id / messages[{role,content,timestamp?}] / user_id / session_id
#   官方 Add 200    : {success:true, request_id, user_id, session_id} —— 三 ID 逐字节回显
#   官方 Search 请求: query / options? / user_id / top_k（正式外部评测固定 100）
#   官方 Search 200 : {data:[{id, content, score?, created_at?}]}，无命中返回空数组
#   内部统一以 scope 命名检索隔离键（官方唯一隔离字段 user_id <-> scope 一一映射）。
# 9/20 官方 Smoke 回执若与 golden 出入：只改 golden 文件与本文件别名表（AliasChoices）。
# ---------------------------------------------------------------------------

class AmlMessage(BaseModel):
    """Add 请求中的消息（官方：Ordered list of messages to store）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    role: str = "user"  # 官方：Message role
    content: str = Field(
        validation_alias=AliasChoices("content", "text", "message"),
        description="官方：Original message text supplied for ingestion.",
    )
    timestamp: int | None = Field(
        default=None,
        validation_alias=AliasChoices("timestamp", "ts", "time"),
        description="官方：Message timestamp in Unix milliseconds when available.",
    )


class AddRequest(BaseModel):
    """官方 Add 请求。平台逐 chunk 发送，返回成功前必须全部存储完毕。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    request_id: str = Field(
        validation_alias=AliasChoices("request_id", "requestId", "id"),
        description="官方：本次写入请求的唯一标识，成功响应必须原样返回该值。",
    )
    messages: list[AmlMessage] = Field(default_factory=list)
    # 内部字段名 scope = 官方 user_id（检索隔离唯一字段）。官方请求只发 user_id，
    # 经 validation_alias 收口；旧客户端发 scope 亦兼容（字段名 + populate_by_name）。
    scope: str = Field(
        validation_alias=AliasChoices("user_id", "userId", "retrieval_scope", "scope"),
        description="官方 user_id：Search 唯一使用的检索范围标识，Add/Search 必须完全一致。",
    )
    session_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("session_id", "sessionId", "conversation_id", "session"),
        description="官方：源会话标识，仅用于分组/组织记忆，不作为 Search 筛选条件。"
        "内部调用可省略（None）；平台总发送该字段。",
    )


class AddResponse(BaseModel):
    """官方 Add 响应：success=true，request_id/user_id/session_id 与请求逐字节一致回显。

    stored/duplicates 为本地统计扩展字段（平台只校验 success 与三 ID 回显；
    官方 docs 明确未声明字段会被忽略，不构成契约风险）。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    success: bool = True  # 官方：Required and must be the boolean value true
    request_id: str = Field(
        validation_alias=AliasChoices("request_id", "requestId", "id")
    )
    user_id: str = Field(
        validation_alias=AliasChoices("user_id", "userId", "scope", "retrieval_scope")
    )
    session_id: str = Field(
        default="",
        validation_alias=AliasChoices("session_id", "sessionId", "conversation_id", "session"),
    )
    stored: int = 0
    duplicates: int = 0


class SearchRequest(BaseModel):
    """官方 Search 请求（每道基准题一次；选择题带 options）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query: str = Field(
        validation_alias=AliasChoices("query", "question", "q"),
        description="官方：按原文检索相关记忆，不得替换为最终答案或使用评测金标。",
    )
    options: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("options", "choices", "answer_choices"),
        description="官方：题目有候选项时传入字符串数组；开放题不发送该字段。",
    )
    scope: str = Field(
        validation_alias=AliasChoices("user_id", "userId", "retrieval_scope", "scope"),
        description="官方 user_id：只能在该值对应的记忆范围内检索（与 Add 完全一致）。",
    )
    top_k: int = Field(
        default=100,
        validation_alias=AliasChoices("top_k", "topk", "topK", "k", "max_results", "limit"),
        description="官方：返回的记忆数量不得超过该值；正式外部评测固定为 100。",
    )


class SearchResult(BaseModel):
    """官方 Search 结果项（data 数组元素）：id/content 必填非空。

    content 为记忆原文，会直接提供给平台统一回答模型；score 越大越相关；
    created_at 可选，记录来源/持久化时间。旧字段 memory/timestamp 保留属性别名。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    content: str = Field(
        validation_alias=AliasChoices("content", "memory", "text"),
        description="官方：Original memory text passed to the platform Answer model.",
    )
    score: float = 0.0  # 官方：可选，数值越大相关性越高
    created_at: str | None = None  # 官方：可选，ISO8601 UTC 来源/持久化时间
    warnings: list[str] = Field(
        default_factory=list, exclude=True, repr=False,
        description="P2-5（2026-09-06）：领域守卫警告放独立字段，content 保持证据原文纯度；"
        "exclude=True 使序列化（官方 data 元素）不含该字段。",
    )

    @property
    def memory(self) -> str:  # 旧消费方兼容（facade/checkup/novel_adapter 曾读 .memory）
        return self.content

    @property
    def timestamp(self) -> int | None:  # 旧消费方兼容：毫秒时间戳（无则 None）
        return None


class SearchResponse(BaseModel):
    """官方 Search 响应：data 为按相关性排序的数组；无相关记忆返回空数组。

    红线：本响应绝不包含 answer 字段——Search 不生成最终答案（业务文档 12.3）。
    官方 docs：不要增加 items 包装层，也不要直接返回顶层数组。
    """

    model_config = ConfigDict(extra="ignore")

    data: list[SearchResult] = Field(default_factory=list)


def to_search_result(mem: MemoryInstance, score: float) -> SearchResult:
    """MemoryInstance -> 官方 SearchResult。created_at 用事件时间，缺失时用入库时间。

    官方 created_at 示例为 ISO8601 UTC（"2026-07-01T12:00:00Z"），故做毫秒->ISO 转换。
    """
    ts_ms = mem.event_ts_ms if mem.event_ts_ms is not None else mem.created_ts_ms
    created: str | None = None
    if ts_ms is not None:
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ms / 1000.0))
    return SearchResult(id=mem.id, content=mem.content, score=round(score, 6), created_at=created)


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    """统一序列化出口（Pydantic v2）。"""
    return model.model_dump(mode="json")

# ---------------------------------------------------------------------------
# 领域适配器模型（novel_adapter / code_adapter 共用）
# ---------------------------------------------------------------------------

class EntityType(str, Enum):
    """业务对象类型。domain=code 时映射为 SERVICE/API/CONFIG/EPIC。"""
    CHARACTER = "CHARACTER"
    ITEM = "ITEM"
    FACTION = "FACTION"
    LOCATION = "LOCATION"
    STORY_THREAD = "STORY_THREAD"


class LifecycleStatus(str, Enum):
    """统一生命周期状态。不同 EntityType 使用不同子集。

    角色: INTRODUCED → ACTIVE → DORMANT → EXITED / REINTRODUCED / TRANSFORMED
    物品: ACQUIRED → HELD → TRANSFERRED → DESTROYED
    故事线: PLANTED → DEVELOPING → CONVERGING → RESOLVED / ABANDONED
    """
    INTRODUCED = "INTRODUCED"
    ACTIVE = "ACTIVE"
    DORMANT = "DORMANT"
    EXITED = "EXITED"
    REINTRODUCED = "REINTRODUCED"
    TRANSFORMED = "TRANSFORMED"
    ACQUIRED = "ACQUIRED"
    HELD = "HELD"
    TRANSFERRED = "TRANSFERRED"
    DESTROYED = "DESTROYED"
    PLANTED = "PLANTED"
    DEVELOPING = "DEVELOPING"
    CONVERGING = "CONVERGING"
    RESOLVED = "RESOLVED"
    ABANDONED = "ABANDONED"


class ResolvedEntity(BaseModel):
    """LLM实体消歧结果。"""
    name: str
    entity_type: EntityType
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    aliases: list[str] = Field(default_factory=list)  # 别名/代词指代


class LifecycleEvent(BaseModel):
    """生命周期事件（不可变记录，类似 Git commit）。

    防幻觉红线：每条事件必须有 evidence（原文片段）+ chapter（来源章节）。
    无证据的状态转换一律拒绝。
    """
    entity_name: str
    entity_type: EntityType
    old_status: LifecycleStatus | None = None
    new_status: LifecycleStatus
    chapter: int
    evidence: str = Field(description="原文片段（截断至200字）")
    detail: dict[str, Any] = Field(default_factory=dict)
    ts_ms: int = Field(default_factory=lambda: int(time.time() * 1000))


class EntitySnapshot(BaseModel):
    """实体当前状态快照（entity_states 表的 Pydantic 映射）。"""
    entity_name: str
    entity_type: EntityType
    status: LifecycleStatus
    first_chapter: int
    last_chapter: int
    appearance_count: int = 0
    current_holder: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    updated_ts_ms: int = 0


class StyleProfile(BaseModel):
    """单章文笔特征（统计量，非主观评价）。"""
    chapter: int
    avg_sentence_len: float = 0.0
    dialogue_ratio: float = 0.0
    paragraph_avg_len: float = 0.0
    rhetoric_density: float = 0.0
    pov: str = "third_limited"
    sample_sentences: list[str] = Field(default_factory=list)
