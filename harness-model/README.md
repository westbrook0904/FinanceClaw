# harness-model

`harness-model` 是 Stage 3A 建立、并由 Stage 3B Router/Planner 使用的模型 Provider 边界。
LLMRouter、LLMPlanner 和未来 Explorer 只依赖 `ModelGateway` 与模型原生协议，不直接依赖
厂商 SDK，也不把模型请求伪装成 Agent/Tool。

## 调用边界

```text
LLMRouter / LLMPlanner / ExplorationEngine
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
- `ModelProvider.features / prepare_structured_output / generate_prepared / bound_input_tokens`：
  受信任能力快照、无损本地 Schema 编译、strict generation 与 sound input token 上界。
- `ModelGateway.generate(...)`：Provider 发现、Health-aware Selection、单 Provider Retry、
  跨 Provider Fallback、完整本地 JSON Schema 校验、finish reason 归一化、attempt accounting
  聚合和 Trace。
- `ModelGateway.prepare_generation(...) / execute_prepared(...)`：冻结所有允许的 retry/fallback
  slots 与 token/cost 上界；只有匹配 reservation receipt 和逐 slot STARTED ticket 后才 outbound。
- `GenerateRequest`：逻辑 model capability ID、messages、legacy response schema 或互斥的
  `StructuredOutputSpec`、
  temperature、max output tokens 和 metadata。
- `GenerateResult`：output、legacy usage、跨 retry/fallback 聚合 accounting、finish reason、
  provider identity、metadata、error 和 trace ID。
- `StructuredGenerationAdapter`：Router/Planner/Explorer 共用的 strict-only 助手，不是新的 SPI。
- `MockFastModel`、`MockQualityModel`、`MockBackupModel`：确定性测试 Provider，支持延迟和
  瞬时失败注入；`MockStrictModelProvider` 额外支持 strict Schema 与资源上界。

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
完整 Draft 2020-12 本地校验、Schema 资源上限/remote ref 阻断、refusal/truncation/filter
归一化、跨 fallback accounting，以及 receipt + slot fencing 的两阶段 budgeted generation。
观察面只记录 `schema_hash`，不记录完整 Schema、Prompt 或原始响应。

Legacy `response_schema` 继续保持 Stage 3A/3B best-effort 语义；REQUIRED 请求不能降级到
legacy `generate()`。LLMPlanner-v2 已迁移到 strict `PlanDraft`；ExplorationEngine 仍属于后续
Stage 3C 步骤。Streaming、vision、embedding、rerank 和真实厂商 SDK adapter 暂缓。

## 测试

```bash
.venv/bin/python -m pytest harness-model/tests -v
```
