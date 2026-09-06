"""ModelProfile 档案单测：解析/参数适配/内容提取（无网络）。"""

from cortexa.llm.profiles import (
    build_chat_kwargs,
    extract_content,
    resolve_profile,
)


def test_resolve_by_model_name():
    assert resolve_profile("deepseek-v4-flash").name == "deepseek"
    assert resolve_profile("kimi-k2.6").name == "kimi"
    assert resolve_profile("moonshot-v1-8k").name == "kimi"
    assert resolve_profile("gpt-4o-mini").name == "openai"
    assert resolve_profile("some-unknown").name == "openai"  # 兜底


def test_resolve_explicit_overrides_auto():
    assert resolve_profile("deepseek-v4-flash", explicit="kimi").name == "kimi"
    assert resolve_profile("anything", explicit="openai").name == "openai"


def test_deepseek_reasoning_min_tokens():
    prof = resolve_profile("deepseek-v4-flash")
    assert prof.is_reasoning and prof.has_reasoning_field
    # 推理模型不允许同步 classify（Add 红线内太慢）
    assert not prof.allow_sync_classify


def test_openai_allows_sync_classify():
    assert resolve_profile("gpt-4o-mini").allow_sync_classify
    assert resolve_profile("some-unknown").allow_sync_classify


def test_deepseek_reasoning_min_tokens_raised():
    prof = resolve_profile("deepseek-v4-flash")
    # classify 调用传 max_tokens=8，档案抬到 >=384（实测推理稳定阈值）
    kwargs = build_chat_kwargs(
        prof, model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "x"}],
        temperature=0, max_tokens=8,
    )
    assert kwargs["max_tokens"] == 384
    assert kwargs["temperature"] == 0  # deepseek 接受 0


def test_kimi_thinking_param():
    prof = resolve_profile("kimi-k2.6")
    kwargs = build_chat_kwargs(
        prof, model="kimi-k2.6",
        messages=[{"role": "user", "content": "x"}],
        temperature=0, max_tokens=8,
    )
    # kimi 不接受 temperature=0（不传）；附加 thinking 禁用
    assert "temperature" not in kwargs
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert kwargs["max_tokens"] == 384


def test_openai_standard_no_extra():
    prof = resolve_profile("gpt-4o-mini")
    kwargs = build_chat_kwargs(
        prof, model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
        temperature=0, max_tokens=8,
    )
    assert kwargs["max_tokens"] == 8  # 标准模型不抬升
    assert kwargs["temperature"] == 0
    assert "extra_body" not in kwargs


class _FakeMsg:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = None


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMsg(content)


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


def test_extract_content_normal():
    prof = resolve_profile("gpt-4o-mini")
    assert extract_content(prof, _FakeResp("  RULE  ")) == "RULE"
    assert extract_content(prof, _FakeResp("")) is None


def test_extract_content_reasoning_only_is_none():
    # 推理模型 content 空但 reasoning 有值 -> 返回 None（防幻觉）
    prof = resolve_profile("deepseek-v4-flash")
    resp = _FakeResp("")
    resp.choices[0].message.reasoning_content = "thinking text"
    assert extract_content(prof, resp) is None
