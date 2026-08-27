# harness-model

`harness-model` 是 Stage 3A 的模型 Provider 边界。Router、Planner 和 Explorer 只依赖
`ModelGateway` 与模型原生协议，不直接依赖厂商 SDK，也不把模型请求伪装成 Agent/Tool。

## 调用边界

```text
LLMRouter / LLMPlanner / ExplorationEngine
                    ↓
               ModelGateway
                    ↓
Registry → Selection / Minimal Health → same-provider Retry / Fallback
                    ↓
               ModelProvider.generate
```

`ModelGateway` 与 `CapabilityInvoker` 共享 Registry、ProviderSelector、
`ProviderExecutionCoordinator`、Trace conventions 和 Execution Events，但不会调用
`CapabilityInvoker.invoke()`。因此模型协议可以稳定保留 messages、model parameters、
structured output、usage、token count 和 finish reason。

## 公共 API

- `ModelProvider.generate(GenerateRequest, InvocationContext) -> GenerateResult`
- `ModelGateway.generate(...)`：Provider 发现、Health-aware Selection、单 Provider Retry、
  跨 Provider Fallback、单次 Provider timeout、错误归一化和 Trace。
- `GenerateRequest`：逻辑 model capability ID、messages、response format/schema、
  temperature、max output tokens 和 metadata。
- `GenerateResult`：output、usage、finish reason、provider identity、metadata、error 和 trace ID。
- `MockFastModel`、`MockQualityModel`、`MockBackupModel`：确定性测试 Provider，支持延迟和
  瞬时失败注入。

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

## 当前范围

第一版支持非流式 `generate`、JSON structured output 顶层形状/required 校验、usage、
timeout/cancellation、fallback、provider identity、Provider Events 和 `MODEL` Span。

Streaming、vision、embedding、rerank、厂商 SDK adapter、完整 JSON Schema validator、
Router/Planner 和 token/cost budget enforcement 暂缓。

## 测试

```bash
.venv/bin/python -m pytest harness-model/tests -v
```
