"""LLM 调用封装（官方硬规则：Add/Search 阶段唯一允许 gpt-4o-mini）。

- chat：OpenAI 兼容接口；未配置 key 时返回 None，调用方必须走规则降级路径
- embed：可插拔提供者（hash=离线确定性 / openai）
- TokenUsage：统一计费口径，写入成本红线（业务文档 12.3：<2000 tokens/条）监控

设计原则：本模块对上层隐藏提供者差异；任何调用点禁止硬编码模型名，
一律取 settings.llm_model（复现验证要求）。
"""

from __future__ import annotations

import hashlib
import math
import re
import threading

from ..config import Settings
from .profiles import build_chat_kwargs, extract_content, resolve_profile
from .resilience import ResilienceRunner, ResilienceStats


class TokenUsage:
    """进程内累计 token 用量（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    def record(self, prompt: int, completion: int) -> None:
        with self._lock:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.calls += 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class HashEmbedder:
    """确定性特征哈希嵌入（hashing trick）。

    词元：小写词 unigram + 字符 3-gram；目标维度 embedding_dim；
    输出 L2 归一化。离线可跑、结果可复现——测试与零成本模式的基座。
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def _bucket(self, token: str) -> int:
        return int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:4], "little") % self.dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        lowered = text.lower()
        words = re.findall(r"[a-z0-9\u4e00-\u9fff]+", lowered)
        tokens: list[str] = []
        tokens.extend(words)
        for word in words:
            if len(word) >= 3:
                tokens.extend(word[i : i + 3] for i in range(len(word) - 2))
        for token in tokens:
            vec[self._bucket(token)] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


class LLMClient:
    """统一 LLM/Embedding 入口。所有调用固定 settings.llm_model。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.usage = TokenUsage()
        # 模型档案：换云端模型只改 .env，零代码改动（见 llm/profiles.py）
        self.profile = resolve_profile(settings.llm_model, settings.llm_profile)
        self._embedder = HashEmbedder(settings.embedding_dim)
        # LLM 韧性观测（独立于 token 用量；/health 消费）
        self.resilience = ResilienceStats()
        # 按场景建 runner（超时/熔断参数从 config 取）
        self._runner_default = ResilienceRunner(
            timeout_ms=settings.llm_timeout_default_ms,
            retry_max=settings.llm_retry_max,
            retry_base_ms=settings.llm_retry_base_ms,
            breaker_threshold=settings.llm_breaker_threshold,
            breaker_cooloff_ms=settings.llm_breaker_cooloff_ms,
            stats=self.resilience,
        )
        self._runner_embed = ResilienceRunner(
            timeout_ms=settings.llm_timeout_embed_ms,
            retry_max=settings.llm_retry_max,
            retry_base_ms=settings.llm_retry_base_ms,
            breaker_threshold=settings.llm_breaker_threshold,
            breaker_cooloff_ms=settings.llm_breaker_cooloff_ms,
            stats=self.resilience,
        )
        self._runner_classify = ResilienceRunner(
            timeout_ms=settings.llm_timeout_classify_ms,
            retry_max=settings.llm_retry_max,
            retry_base_ms=settings.llm_retry_base_ms,
            breaker_threshold=settings.llm_breaker_threshold,
            breaker_cooloff_ms=settings.llm_breaker_cooloff_ms,
            stats=self.resilience,
        )
        self._openai_client = None
        # chat(分类/抽取) 与 embedding 解耦：只要配了 key 就建 OpenAI 兼容客户端。
        # （embedding 用哪个 provider 由 embed() 内部决定；DeepSeek 只有 chat 无 embedding）
        if settings.openai_api_key:
            from openai import OpenAI  # 延迟导入：无 key 环境不依赖

            self._openai_client = OpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url or None,
                # 韧性层自己管超时；SDK 也设宽松上限防线程永久挂起
                timeout=max(5.0, settings.llm_timeout_default_ms / 1000.0),
                max_retries=0,  # 重试由韧性层统一管（避免 SDK 内部重试放大延迟）
            )

    # -- LLM ----------------------------------------------------------------

    def available(self) -> bool:
        """LLM 是否可用（无 key 时调用方走规则降级路径）。"""
        return bool(self.settings.openai_api_key)

    def can_sync_classify(self) -> bool:
        """当前模型是否允许在 Add 同步红线内做 LLM 兜底分类。

        推理模型（deepseek-v4-flash/kimi）reasoning 2-4s，无法纳入 Add 500ms
        预算——规则引擎优先本就是设计，仅非推理模型允许同步兜底。
        """
        return self.profile.allow_sync_classify

    def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        temperature: float = 0,
        deadline_ms: int | None = None,
        use_classify_budget: bool = False,
    ) -> str | None:
        """单轮补全（接韧性层：超时/重试/熔断）。

        失败/不可用/熔断/超预算一律返回 None——调用方必须走规则降级路径
        （绝不抛出阻断管线）。

        deadline_ms: 调用方剩余预算（红线场景：Add/Search 内收紧超时）。
        use_classify_budget: True 时用 Add 内 classify 的紧超时 runner
            （rule_wing 兜底分类专用）。
        """
        if not self.available():
            return None

        runner = (
            self._runner_classify if use_classify_budget else self._runner_default
        )

        def _call():
            client = self._openai_client
            if client is None:
                raise RuntimeError("openai client not initialized")
            # 档案适配：max_tokens 余量 / temperature / thinking 参数
            # （调用方无需感知模型差异，切模型只改 .env）
            kwargs = build_chat_kwargs(
                self.profile,
                model=self.settings.llm_model,  # 官方硬规则：仅 gpt-4o-mini
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return client.chat.completions.create(**kwargs)

        result = runner.run(_call, deadline_ms=deadline_ms)
        if not result.ok or result.value is None:
            return None

        resp = result.value
        # 档案提取：推理模型 content 空时返回 None（防把 reasoning 当答案）
        text = extract_content(self.profile, resp)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.usage.record(
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
            )
        return text

    # -- Embedding ----------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。hash 提供者零成本同步；openai 提供者失败时降级 hash。

        维度一致性红线：存储层按 settings.embedding_dim 建矩阵，openai 调用必须
        显式传 dimensions= 把原生 1536 维降到目标维（text-embedding-3 系列支持
        Matryoshka 截断），否则重启后 _ensure_vec_cache 按维度过滤会把全部向量
        静默丢弃——语义通道瘫痪且无报错。
        """
        if self.settings.embedding_provider == "openai" and self._openai_client is not None:
            def _call():
                client = self._openai_client
                if client is None:
                    raise RuntimeError("openai client not initialized")
                kwargs: dict = {"model": self.settings.embedding_model, "input": texts}
                if self.settings.embedding_model.startswith("text-embedding-3"):
                    kwargs["dimensions"] = self.settings.embedding_dim
                return client.embeddings.create(**kwargs)

            # 韧性执行：超时/重试/熔断由 runner 管理；失败降级 hash（下方兜底）
            result = self._runner_embed.run(_call)
            if result.ok and result.value is not None:
                resp = result.value
                ordered = sorted(resp.data, key=lambda d: d.index)
                vectors = [d.embedding for d in ordered]
                self.usage.record(0, 0)  # embedding 用量不计入 completion 口径
                return vectors
            # 失败/熔断/超时 -> 降级 hash（保证写入管线不因外部服务中断）
        return [self._embedder.embed(t) for t in texts]
