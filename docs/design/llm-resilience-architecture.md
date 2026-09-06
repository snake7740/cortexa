# LLM 调用韧性架构（超时/重试/熔断/降级）

> 版本: v1.0 | 日期: 2026-09-03 | 状态: 已确认，待实现
> 关联: config.py 红线(12.3) / llm/client.py / rule_wing / search / pipeline

## 一、问题陈述

现有 LLM 调用存在系统性韧性缺陷，实战已复现（Kimi API 超时导致联调卡死 10 分钟+）：

| 缺陷 | 现状（代码实证） | 后果 |
|------|----------------|------|
| 无超时 | client.chat.completions.create() 未传 timeout，OpenAI SDK 默认 60s | 一次慢响应 = Add/Search 挂死 |
| 无重试 | 失败直接返回 None | 瞬时抖动直接降级，可用性损失 |
| 无熔断 | 连续失败仍无限重试 | 雪崩：评测时每个请求都等满超时 |
| 无降级预算 | 调用方各自处理 None，无统一口径 | Add 500ms 红线无保障 |
| 无观测 | 只有 token 计数，无延迟/失败/降级统计 | 无法定位瓶颈 |

**比赛场景**：正式评测使用 gpt-4o-mini（平台侧），同样可能超时/限流(429)/服务抖动。
红线硬约束：Add <500ms、Search P95 <200ms（业务文档 12.3，config 已声明）。

## 二、设计原则

1. **LLM 是可选增强，不是依赖**：任何路径 LLM 失败/超时都必须能降级到确定性结果继续。
2. **预算先于正确性**：Add/Search 的延迟红线是硬约束，宁可降级不可超时。
3. **快速失败**：不是无限等待后失败，而是按预算尽快失败并降级。
4. **分层防御**：超时(每调用) -> 重试(瞬时) -> 熔断(持续) -> 降级(兜底) 四层。
5. **可观测**：每层都有计数，/health 可见。

## 三、架构总览

    调用方 --> LLMClient（韧性层，签名不变）
      |  timeout: 每调用硬上限
      |  retry:   瞬时错误重试(指数退避)
      |  breaker: 连续失败熔断
      |  fallback: 超预算 -> None/hash
      v
    OpenAI SDK（gpt-4o-mini / embedding）
      |
      +-- 成功(返回结果)     失败(记录 -> 走降级)

调用方职责（不变）：
- rule_wing.classify_message: LLM None -> 规则结果兜底（已有）
- search/pipeline embed:     LLM 失败 -> hash 嵌入兜底（已有）
- 新增：调用方上报 deadline，韧性层按 deadline 收紧 timeout

## 四、四层防御设计

### 4.1 Timeout（每调用超时）

**目标**：单次 LLM 调用的硬时间上限。

| 场景 | 默认超时 | 说明 |
|------|---------|------|
| Add 内 classify 兜底 | add_budget_ms(500) x 0.5 = 250ms | 与 Add 总预算联动 |
| Search 内 embed | search_p95_ms(200) x 0.5 = 100ms | 与 Search P95 联动 |
| 后台/非阻塞(实体消歧) | 5s | post_ingest 不阻塞主响应 |

实现：openai SDK timeout= 参数 + 客户端兜底计时。
**关键**：timeout 由 config 驱动，不是硬编码——红线调整一处生效。

### 4.2 Retry（瞬时错误重试）

| 项 | 值 |
|----|----|
| 重试次数 | 1 次 |
| 重试条件 | 仅 429/5xx/连接错误/超时 |
| 不重试 | 4xx(除429)/认证失败/本地参数错误 |
| 退避 | 指数退避，base 50ms，最多 200ms |
| 预算联动 | 重试会让总耗时超 deadline 则跳过直接降级 |

### 4.3 CircuitBreaker（熔断）

状态机: CLOSED(正常) -> OPEN(熔断) -> HALF_OPEN(试探) -> CLOSED/OPEN

- CLOSED:   连续失败 >= failure_threshold(3) -> OPEN
- OPEN:     冷却期 open_cooloff(5s) 内直接失败 -> HALF_OPEN
- HALF_OPEN: 放行 1 个试探请求；成功 -> CLOSED | 失败 -> OPEN

| 参数 | 默认值 | 说明 |
|------|-------|------|
| failure_threshold | 3 | 连续失败次数 |
| open_cooloff_ms | 5000 | 熔断冷却期 |
| 线程安全 | RLock | FastAPI 多线程 |

### 4.4 Fallback（降级）

| 调用 | 降级路径 | 现有支持 |
|------|---------|---------|
| chat (classify) | 规则引擎分类结果 | 已有 |
| chat (实体消歧) | 不写实体/跳过 | 已有 |
| embed | HashEmbedder 确定性嵌入 | 已有 |

**降级必须可观测**：记录 fallback 次数，防止静默质量下降。

## 五、调用点 deadline 传导

调用方在需要满足红线时传入 deadline_ms（剩余预算），韧性层：
effective_timeout = min(default_timeout, deadline_ms x 0.7)  # 留 30% 给后续

- pipeline.run_add: 传剩余预算
- search.search_memory: 传剩余预算
- novel_adapter（后台）: 不传 = 用默认 5s

## 六、观测指标（/health 扩展）

| 指标 | 意义 |
|------|------|
| llm_calls / llm_failures | 成功率 |
| llm_timeouts / llm_retries | 超时与重试 |
| llm_breaker_state | 熔断状态 |
| llm_fallbacks | 降级次数（质量损失预警） |
| llm_p95_ms | 成功调用 P95 延迟 |

## 七、Config 扩展

```python
llm_timeout_default_ms: int = 5000     # 后台/非阻塞默认
llm_timeout_embed_ms: int = 300        # Search embed 预算
llm_timeout_classify_ms: int = 400     # Add classify 兜底预算
llm_retry_max: int = 1                 # 最大重试
llm_retry_base_ms: int = 50
llm_breaker_threshold: int = 3         # 连续失败熔断
llm_breaker_cooloff_ms: int = 5000
```

## 八、实现文件

- cortexa/llm/resilience.py: 新模块（Timeout/Retry/Breaker 纯逻辑，可单测）
- cortexa/llm/client.py: LLMClient 改造（内部加韧性，签名不变）
- cortexa/config.py: 新增韧性参数
- cortexa/server.py: /health 扩展观测
- tests/test_llm_resilience.py: 单测（mock OpenAI SDK）

## 九、验收标准

1. 超时：mock SDK sleep > timeout -> 按时返回 None，不挂死
2. 重试：首次 429/5xx 第二次成功 -> 返回结果，retries=1
3. 熔断：连续 3 失败 -> 第 4 次直接失败不调用 SDK；冷却后 HALF_OPEN 恢复
4. 降级：LLM 不可用时 Add/Search 全链路正常（hash + 规则）
5. 红线：模拟慢 LLM，Add/Search 仍返回（降级）不超预算 +50ms
6. 观测：/health 输出新指标

## 十、风险与缓解

| 风险 | 缓解 |
|------|------|
| timeout 太短误杀 | 默认值从宽，红线场景收紧 |
| 熔断误判 | threshold=3 容忍 2 次抖动 |
| 重试放大延迟 | 预算联动：超 deadline 不重试 |
| 降级静默 | 降级计数 + journal 记录 |