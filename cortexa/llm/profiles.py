"""LLM Provider 档案：把不同云端模型的调用差异收敛为声明式配置。

设计文档：docs/design/llm-resilience-architecture.md（韧性）+
本模块解决"换模型要改代码"的适配层问题（2026-09-03 实证驱动）：

差异矩阵（Kimi k2.6 / DeepSeek v4-flash / gpt-4o-mini 实测）：
| 维度 | 标准(openai) | deepseek-v4-flash | kimi-k2.6 |
|------|-------------|-------------------|-----------|
| 推理先行(reasoning) | 否 | 是 | 是 |
| reasoning_content 字段 | 无 | 有 | 有 |
| 最小 max_tokens | 小(8可用) | >=128 | >=128 |
| temperature=0 | 接受 | 接受 | 部分拒绝 |
| thinking 禁用参数 | 不需要 | 不需要 | 需要 |

使用：LLMClient 构造时按 settings.llm_profile（或模型名自动匹配）解析档案，
chat() 内部据档案自动适配——调用方不需要关心用的是哪家模型。
新增模型 = 在此注册表加一行，零代码改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelProfile:
    """单个模型/模型族的调用行为档案。"""

    name: str                     # 档案名
    is_reasoning: bool = False    # 推理模型：reasoning 先消耗 token 配额
    has_reasoning_field: bool = False  # 响应带 reasoning_content 字段
    min_completion_tokens: int = 128   # 最小 max_tokens（推理余量）
    temperature_zero_ok: bool = True   # 是否接受 temperature=0
    thinking_param: str | None = None  # 需要禁思考的 extra_body 参数名
    # 附加请求体参数（如 kimi thinking={"type":"disabled"}）
    extra_body: dict | None = field(default_factory=dict)
    # 是否允许在 Add 同步红线内做 LLM 兜底分类。
    # 推理模型 reasoning 2-4s + 384+ tokens，实测无法纳入 Add 500ms 预算 -> False
    # （规则引擎优先本就是设计；LLM 分类仅在非红线场景可用）
    allow_sync_classify: bool = True


# ---------------------------------------------------------------------------
# 档案注册表：新增模型在此加一行
# ---------------------------------------------------------------------------

PROFILES: dict[str, ModelProfile] = {
    # 标准 OpenAI 兼容（gpt-4o-mini 等非推理）
    "openai": ModelProfile(
        name="openai",
        is_reasoning=False,
        min_completion_tokens=8,
    ),
    # DeepSeek V4-Flash（推理模型：reasoning 先行）
    # 2026-09-03 实测：reasoning 长度波动 500-1071 tokens，
    # max_tokens<384 时 reasoning 常被截断致 content 空（mt=128: 1/3, 256: 2/3, 384: 3/3）
    "deepseek": ModelProfile(
        name="deepseek",
        is_reasoning=True,
        has_reasoning_field=True,
        min_completion_tokens=384,
        temperature_zero_ok=True,
        allow_sync_classify=False,  # 推理模型太慢，Add 红线内不做 LLM 兜底
    ),
    # Kimi k2.6（推理模型 + 需显式禁思考参数；实测同须大余量）
    "kimi": ModelProfile(
        name="kimi",
        is_reasoning=True,
        has_reasoning_field=True,
        min_completion_tokens=384,
        temperature_zero_ok=False,  # 实测部分模型只接受 temperature=1
        allow_sync_classify=False,  # 同 deepseek：推理模型不做同步兜底
        extra_body={"thinking": {"type": "disabled"}},
    ),
}

# 模型名 → 档案名 自动匹配表（llm_profile=auto 时用）
_MODEL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("deepseek", "deepseek"),
    ("kimi", "kimi"),
    ("moonshot", "kimi"),
    ("gpt-", "openai"),
)


def resolve_profile(model: str, explicit: str | None = None) -> ModelProfile:
    """解析模型档案。

    explicit: settings.llm_profile（openai/deepseek/kimi/auto）。
    - 显式指定且存在 -> 用之
    - auto / 未知 -> 按模型名自动匹配（子串），兜底 openai 标准档案
    """
    if explicit and explicit != "auto" and explicit in PROFILES:
        return PROFILES[explicit]
    lowered = (model or "").lower()
    for marker, prof_name in _MODEL_PATTERNS:
        if marker in lowered:
            return PROFILES[prof_name]
    return PROFILES["openai"]


def build_chat_kwargs(
    profile: ModelProfile,
    *,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
) -> dict:
    """按档案生成 chat.completions.create 的 kwargs。

    适配规则（全部由档案驱动，调用方无需感知模型差异）：
    1. max_tokens 下限抬升：推理模型给足 reasoning 余量；
    2. temperature：档案不允许 0 时用默认（不传），允许则传原值；
    3. extra_body：档案需要的 thinking 禁用等参数自动附加。
    """
    kwargs: dict = {"model": model, "messages": messages}
    # 1) max_tokens 余量
    effective_max = max(max_tokens, profile.min_completion_tokens)
    kwargs["max_tokens"] = effective_max
    # 2) temperature
    if profile.temperature_zero_ok:
        kwargs["temperature"] = temperature
    # 3) extra_body（thinking 禁用等）
    if profile.extra_body:
        kwargs["extra_body"] = profile.extra_body
    return kwargs


def extract_content(profile: ModelProfile, resp) -> str | None:
    """从响应提取文本。推理模型 content 可能为空（reasoning 吃满配额）。"""
    if not resp or not resp.choices:
        return None
    msg = resp.choices[0].message
    content = getattr(msg, "content", None)
    if content and content.strip():
        return content.strip()
    # content 空：推理模型可能只剩 reasoning_content——不当作答案返回
    # （防幻觉：reasoning 不是给调用方的最终文本），返回 None 让调用方降级
    return None
