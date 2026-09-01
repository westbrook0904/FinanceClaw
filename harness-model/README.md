# harness-model

`harness-model` 是 Stage 3A 建立、并由 Stage 3B Router/Planner 使用的模型 Provider 边界。
LLMRouter、LLMPlanner 和 ExplorationEngine 通过 `StructuredGenerationAdapter` 使用
`ModelGateway` 与模型原生协议，不直接依赖
厂商 SDK，也不把模型请求伪装成 Agent/Tool；具体 Provider adapter 可以使用厂商官方 SDK。

## 调用边界

```text
LLMRouter / LLMPlanner / ExplorationEngine
                    ↓
        StructuredGenerationAdapter（strict-only）
                    ↓
               ModelGateway
                    ↓
Registry feature snapshot → strict Schema preparation → Selection / Retry / Fallback
                    ↓
      ModelProvider.generate / generate_prepared
```

`ModelGateway` 与 `CapabilityInvoker` 共享 Registry、ProviderSelector、
`ProviderExecutionCoordinator`、Trace conventions 和 Execution Events，但不会调用
`CapabilityInvoker.invoke()`。因此模型协议可以稳定保留 messages、model parameters、
structured output、usage、token count 和 finish reason。

## 公共 API

- `ModelProvider.generate(GenerateRequest, InvocationContext) -> GenerateResult`
- `ModelProvider.features / prepare_structured_output / generate_prepared`：
  受信任能力快照、无损本地 Schema 编译与 strict generation。
- `ModelGateway.generate(...)`：Provider 发现、Health-aware Selection、单 Provider Retry、
  跨 Provider Fallback、完整本地 JSON Schema 校验、finish reason 归一化、attempt accounting
  聚合和 Trace。
- `ModelGateway.prepare_generation(...) / execute_prepared(...)`：冻结所有允许的 retry/fallback
  slots、授权上下文 hash 与 Provider incarnation；只有匹配 reservation receipt 和逐 slot
  STARTED ticket 后才 outbound。跨 Tenant/Identity/Request/执行引用复用或热替换 Provider 实例
  都会在网络调用前 fail-closed。
- `GenerateRequest`：逻辑 model capability ID、messages、legacy response schema 或互斥的
  `StructuredOutputSpec`、
  temperature、max output tokens 和 metadata。
- `GenerateResult`：output、legacy usage、跨 retry/fallback 聚合 accounting、finish reason、
  provider identity、metadata、error 和 trace ID。
- `StructuredGenerationAdapter`：Router/Planner/Explorer 共用的 strict-only 助手，不是新的 SPI。
- `MockFastModel`、`MockQualityModel`、`MockBackupModel`：确定性测试 Provider，支持延迟和
  瞬时失败注入；`MockStrictModelProvider` 额外支持 strict Schema。
- `OpenAIResponsesModelProvider`：Foundation F5 的真实非流式 adapter，通过官方
  `openai.AsyncOpenAI.responses.create()` 调用 Responses API；SDK 负责 HTTP、认证、响应解码和
  OpenAI-compatible 异常，Harness 继续负责 retry/fallback、usage 与安全错误归一化。
- 该 adapter 不向服务端发送 `max_output_tokens`。可通过构造参数或
  `OPENAI_REASONING_EFFORT` 设置 `reasoning.effort`；启用思考时不发送 `temperature`，显式
  `none` 关闭思考后才保留 temperature。响应只消费 SDK 汇总的最终 `output_text`，不把
  reasoning item/思维链写入 Result、Trace 或报告；仅将 `reasoning_tokens` 作为安全计量 metadata。
- Provider 默认 `store=false`。无 map-valued `additionalProperties` 的 Schema 使用
  `text.format=json_schema`；存在该类跨 Provider 不兼容字段时使用 `json_object`，完整原始 Schema
  仍由 ModelGateway 在结果进入 Router/Planner/Explorer 前强制校验。Provider adapter 不改写调用方
  messages；JSON mode 下所需的紧凑输出契约由 Router/Planner/Explorer 各自拥有的 system prompt
  提供。Harness Contract 冻结产生的 `mappingproxy` / `tuple` 会在校验时转换为临时 `dict` / `list`
  视图，不改变 Result 的不可变性。

## 注册和调用

Model Provider 使用 `CapabilityType.MODEL` 注册到共享 Registry。`provider_id` 属于
`ProviderDescriptor`，Gateway 返回实际选中的注册身份：

```python
from harness_bootstrap import build_harness
from harness_contracts import ProviderDescriptor, Request, RequestInput
from harness_model import (
    DEFAULT_MODEL_CAPABILITY_ID,
    GenerateRequest,
    MockQualityModel,
    ModelMessage,
    ModelRole,
)

app = build_harness(entry_point_group=None)
provider = MockQualityModel()
app.registry.register_provider(
    provider,
    descriptor=ProviderDescriptor(
        provider_id="quality-provider",
        capability_id=DEFAULT_MODEL_CAPABILITY_ID,
        plugin_id="model-adapters",
        implementation_version="1.0.0",
        priority=100,
    ),
)
context = app.components.context_factory.create(
    Request(input=RequestInput(type="text", content="hello"))
)
result = await app.model_gateway.generate(
    GenerateRequest(
        model=DEFAULT_MODEL_CAPABILITY_ID,
        messages=(ModelMessage(role=ModelRole.USER, content="hello"),),
    ),
    context,
)
```

`timeout_ms` 是单次 Provider attempt 的超时，因此 Quality Provider 超时后仍可按
`fallbackable` 语义切换 Backup；`deadline_at` 和 `InvocationContext.deadline_at` 是整个
生成流程共享的绝对截止时间，不会因 Retry/Fallback 重置。

MODEL Span 保留稳定错误码和固定错误摘要，不复制 Provider 返回的原始错误消息、模型响应
或 Prompt；完整结构化错误仍只通过受控的 `GenerateResult` 返回给调用边界。

## 当前范围

Stage 3C Step 2 已支持非流式 strict structured output、Provider-specific 无损 preparation、
完整 Draft 2020-12 本地校验（包含 primitive-root JSON）、Schema 资源上限/remote ref 阻断、refusal/truncation/filter
归一化、跨 fallback usage accounting，以及 receipt + slot fencing 的两阶段 reserved generation。
reservation 只冻结 Provider attempt slots，不承担 token、成本或耗时预算。
观察面只记录 `schema_hash`，不记录完整 Schema、Prompt 或原始响应。

Legacy `response_schema` 继续保持 Stage 3A/3B best-effort 语义；REQUIRED 请求不能降级到
legacy `generate()`。LLMRouter-v2 已迁移到最小 route completion Draft，LLMPlanner-v2 已迁移到
strict `PlanDraft`，两者与 ExplorationEngine 共用 strict-only adapter。F5 的
`OpenAIResponsesModelProvider` 直接依赖官方 OpenAI Python SDK；协议映射依据 OpenAI Responses API 的
[`POST /responses`](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
以及 SDK 的 `responses.create()`；DeepSeek 思考模式兼容行为依据
[`reasoning.effort` 与 reasoning output](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)。
当前调用不使用 Responses tool call，因此无需跨轮回传 provider reasoning；未来若在单次 Responses
会话中开放工具调用，必须先补齐 reasoning replay contract。Streaming、vision、embedding、rerank
和其他厂商 adapter 暂缓。

## 测试

```bash
.venv/bin/python -m pytest harness-model/tests -v
```
