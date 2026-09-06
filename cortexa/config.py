"""Cortexa 集中配置（Pydantic Settings）。

所有阈值/红线/模型名集中于此，与业务文档 12.3 的硬约束一一对应。
官方硬规则：Add/Search 阶段唯一允许的 LLM 为 gpt-4o-mini（平台复现验证），
因此本系统所有内部 LLM 调用固定使用 ``llm_model``，不设高级模型层级。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CORTEXA_", env_file=".env", extra="ignore")

    # ---- 存储与运行 ----
    data_dir: Path = Path("./data")
    api_key: str = ""  # 留空 = 无需认证（官方 Authentication 支持 No authentication）

    # ---- LLM（官方硬规则：固定 gpt-4o-mini）----
    llm_model: str = "gpt-4o-mini"
    openai_api_key: str = ""
    openai_base_url: str = ""  # OpenAI 兼容端点，留空用官方默认
    # LLM provider 档案（模型行为差异声明式配置，见 llm/profiles.py）：
    #   openai   = 标准模型（gpt-4o-mini）
    #   deepseek = 推理模型 deepseek-v4-flash（reasoning 先行）
    #   kimi     = 推理模型 kimi-k2.6（需 thinking 禁用参数）
    #   auto     = 按模型名自动匹配（默认）
    llm_profile: str = "auto"
    # 推理模型最小 max_tokens：reasoning 会先消耗配额，classify 等小输出任务
    # 必须给足余量（实测 deepseek-v4-flash mt>=128 才稳定返回 content）
    llm_min_completion_tokens: int = 128
    # 检索增强重编码 RARE-lite：检索命中记忆两两关联写回（维度B簇间桥梁）
    rare_enabled: bool = True
    rare_top_n: int = 5  # 每次检索最多关联的记忆数
    # 关系自动建边（RelationInferrer）：原文显式关系 -> graph 边。默认关闭：
    # 自动建边是行为改变，A/B 验证收益后开启（novel_adapter.infer_relations 已就绪）
    relation_inferrer: bool = False
    # 双阶段 Add 异步增强（独立于 journal_mode——Review 修正2）：Add 返回后补实体/关系
    async_enhance_enabled: bool = True
    # 增强用已知实体名单（Cast 前用种子名单；从场景 schema/调用方提供）

    # ---- LLM 韧性（超时/重试/熔断；设计文档 docs/design/llm-resilience-architecture.md）----
    llm_timeout_default_ms: int = 5000       # 后台/非阻塞调用默认超时
    llm_timeout_embed_ms: int = 300          # Search embed 预算（联动 search_p95_ms 红线）
    llm_timeout_classify_ms: int = 400       # Add classify 兜底预算（联动 add_budget_ms）
    llm_retry_max: int = 1                   # 最大重试次数（瞬时错误）
    llm_retry_base_ms: int = 50              # 重试退避基值
    llm_breaker_threshold: int = 3           # 连续失败熔断阈值
    llm_breaker_cooloff_ms: int = 5000       # 熔断冷却期

    # ---- Embedding ----
    embedding_provider: str = "hash"  # hash（离线确定性）| openai
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 512  # 全提供者统一维度：openai 调用显式传 dimensions=（Matryoshka 降维）

    # ---- 检索 ----
    default_top_k: int = 5
    max_top_k: int = 200  # 冒烟测试会传 Top K 90，须允许 >=90
    rrf_k: int = 60  # RRF 常数（业务文档 8.x 三流融合思路）
    vector_min_cos: float = 0.05  # 语义通道相关性下限：低于此值不进入融合（precision-first）
    # hash 嵌入专用下限（2026-09-04 维度 G 修复）：hash 词袋余弦 = 词重叠比例，
    # 语义嵌入 0.05 对 hash 过松——实测无关对 0.03-0.16、相关对 0.38+，0.25 干净分离。
    # openai 语义嵌入仍用 vector_min_cos=0.05；按 embedding_provider 自动选择。
    vector_min_cos_hash: float = 0.28
    # 实体特征访问通道（维度 B 前哨）：代码保留，默认关闭。
    # 2026-08-31 A/B：内置题集与小说 1736 条库 Δ=0，延迟 15→54ms。
    # 2026-09-06 复测（2620 条库 + 11 题机检，见 .loop/entity_ab_report.json）：
    #   off=9/11@20.5ms；on(现有)=9/11@75.1ms（3.7× 无增益）；内容-专名 LIKE 原型=9/11@21.2ms（零增益）。
    # 两处未中（灵根/甲字）均为跨词汇/覆盖缺口，非实体通道可解——真实提分路径 =
    # P4 中文专名 NER + 语义嵌入；本地确定性通道到顶，按"实际使用先检验"维持 False。
    entity_channel: bool = False
    # 编号精确通道（真实使用痛点：小说"第 107 章讲了什么"、编号检索）：
    # 查询命中"第 N 章 / Ch N"模式 → 按章节锚点精确取回对应记忆，进 RRF 融合。
    chapter_channel: bool = True
    # R(t)×retrieval_weight 融合调制强度（w3_fsrs_wire）：factor = 1 + w*(R_eff - 1) ∈ [1-w, 1]。
    # 只下调（老化衰减的非 RULE 记忆），绝不放大相关性；未复习记忆 R_eff 视为 1，
    # factor 恒等 1.0——首次评测（无任何复习）排序与调制前逐字节一致，零退化。RULE/no_decay 强制中性。
    retrieval_fusion_weight: float = 0.15

    # ---- 业务红线（业务文档 12.3）----
    add_budget_ms: int = 500  # 已接线：/health add_within_budget 自证
    search_p95_ms: int = 200  # 已接线：/health search_within_budget 自证
    # P2-4（2026-09-06）：evidence_token_cap/write_token_budget/consolidation_core_level/
    # forget_cosine_max/forget_llm_relevance_max 等未接线阈值已从 Settings 移除（无消费方，
    # 禁止只声明）；对应机制（证据截断/写入分块/核心记忆区/遗忘引擎）落地 P3/P4 时随消费
    # 方一起引入，设计依据见 docs/research 2026-08-31 论证。

    # ---- 巩固调度（业务文档 8.1，FSRS 简化参数）----
    request_retention: float = 0.9  # FSRS 目标留存率（touch_retrieval 强化基准）

    # ---- 场景（L3 schema 选择：novel/aml/code/custom）----
    scene: str = "novel"  # 决定治理/增强/生命周期用哪个场景 schema
    # 写入期治理（AML 维度 D）：Add 时同主体新旧陈述冲突触发 supersede
    governance_enabled: bool = True

    # ---- 规则专属 Wing（业务文档 8.2 / 8.3）----
    rule_llm_fallback: bool = True  # 规则引擎未命中时是否用 LLM 兜底判定
    rule_boost: float = 1.25  # RULE 记忆融合分加成（维度 F：规则稳定可见；只影响排序不扩大召回）

    # ---- 任务流水（journal.py；官方合规：评测数据不留日志 -> eval 模式自动脱敏）----
    journal_mode: str = "app"  # off=不记录 | app=全量（真实使用） | eval=只留元数据（参赛评测）

    # ---- 领域适配器（novel_adapter.py）----
    novel_adapter_enabled: bool = False       # 总开关（默认关闭，不影响AML评测）
    adapter_entity_resolver: bool = True      # LLM实体消歧（替代启发式抽取）
    adapter_character_tracker: bool = True    # 角色生命周期追踪
    adapter_style_profiler: bool = True       # 文笔特征提取（零LLM）
    adapter_relation_inferrer: bool = False   # 关系推断（P2，默认关闭）
    adapter_lifecycle_guard: bool = True      # 检索时EXITED角色预警
    adapter_dormant_threshold: int = 10       # >N章未出现 → DORMANT


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例配置。测试中可通过 get_settings.cache_clear() 重置。"""
    return Settings()