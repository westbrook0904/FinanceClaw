# harness-runtime

`harness-runtime` 提供 Request 级 Direct Invocation 生命周期，以及 Direct Runtime
和 ExecutionEngine 共同使用的 `CapabilityInvoker` 受控调用边界。Scheduler、恢复
逻辑和其他调用方不得绕过 Invoker 直接执行 Registry Provider。

## 公共 API

- `HarnessRuntime.invoke(Request) -> ResultEnvelope`：兼容阶段一的单 Capability
  Direct Invocation。
- `CapabilityInvoker.invoke(capability_id, input, context, ...) -> ResultEnvelope`：
  可供 Plan Node 复用的统一调用入口。
- `ProviderExecutionCoordinator`：区分同 Provider Retry 与跨 Provider Fallback，
  并统一执行 Deadline、幂等和 equivalence group 安全校验。
- `ProviderResumeState`：携带 checkpointed ProviderAttempt、最近结果和已尝试 Provider
  集合，使 Resume 固定原 Provider，并避免重新选择或重复已完成 attempt。
- `InvocationLifecycle`：共享 Context 创建、Trace 传播、结果 trace ID 与 Span
  收尾语义。
- Provider observability：发布 candidates/selected/retrying/fallback/failed 事件，并为
  每次初始选择或 fallback 选择创建 `PROVIDER_SELECT` Span。
- `InvocationContextFactory.create(Request) -> InvocationContext`。
- `DefaultInvocationContextFactory`：构造最小可信 Context，并按 Request
  `timeout_ms` 计算绝对 deadline。

## Direct Invocation

```text
Request（target required）
  ↓
ContextFactory.create
  ↓
REQUEST / RUNTIME
  ↓
CapabilityInvoker
  ├── Registry.candidates → Provider Selection
  └── ProviderExecutionCoordinator
      ├── selected Provider 内 Retry
      ├── fallbackable failure 后重新 Selection
      ├── PRE_EXECUTE Policy
      ├── CAPABILITY → AGENT / TOOL
      └── deadline / error / result normalization
  ↓
ResultEnvelope
```

`Request.target` 在协议层对 Plan 请求可为空，但 `HarnessRuntime.invoke()` 仍严格要求
target，缺失时返回 `HARNESS.REQUEST.TARGET_REQUIRED`。target 中可选的 `plugin`
会作为 Registry Provider 来源限定条件。

## CapabilityInvoker 语义

Invoker 与 ProviderExecutionCoordinator 共同保证：

1. 按 Capability/Plugin ID 发现候选并确定初始 Provider。
2. Retry 始终保持当前 Provider；只有 `fallbackable=true` 才重新选择剩余候选。
3. 每次实际 Provider 调用都以可信 InvocationContext 和 ProviderDescriptor 评估
   PRE_EXECUTE Policy。
4. WRITE Retry 要求 Capability 支持幂等且存在稳定 key；跨 Provider Fallback 还要求
   source/target 具有相同的非空 `equivalence_group`。
5. 创建 Registry、Provider Select、Policy、Capability、Agent/Tool Trace，并把
   RequestInput 适配为 `AgentRequest` 或结构化 `ToolRequest`；retry/fallback 同时记录
   可解释的 Trace Event 与 ExecutionEvent。
6. 所有 ProviderAttempt 共享同一个绝对 Deadline，验证 Descriptor/SPI 类型和
   `ResultEnvelope`，并归一化执行错误。

Policy DENY 返回 `DENIED`；Plan 上的 REQUIRE_APPROVAL 返回 `ACCEPTED` 供
ExecutionEngine 持久化等待，Direct Invocation 则返回明确 DENIED。调用方取消 task 时，
开放 Span 会关闭为 CANCELLED，`CancelledError` 继续向上传播。

## Agent 与 Tool 适配

- Agent 接收 `AgentRequest(input=input)`。
- Tool 要求 `RequestInput.content` 是 JSON object，并转换为
  `ToolRequest(arguments=content)`。
- Provider SPI 类型必须与 Descriptor 的 `CapabilityType` 一致。
- Provider 必须返回 `ResultEnvelope`，否则返回
  `HARNESS.CAPABILITY.INVALID_RESULT`。

## Context 与安全边界

默认 Factory 不把 Request 的 `user_id`、`tenant_id` 提升为可信 Identity/Tenant。
认证和租户解析应在应用边界完成，并通过自定义 `InvocationContextFactory` 注入。
ExecutionEngine 会为 Plan 调用附加受控的 plan/node/idempotency 和 Approval Grant
属性；调用方提供的同名保留属性不会被信任。

## 依赖边界

Runtime 可以依赖 Contracts、SPI、Registry、Selection、Policy、Trace 和 Events；不依赖具体插件、StateStore
或 ExecutionEngine，也不负责 DAG、Checkpoint、Approval 协调和插件生命周期，这些职责
位于独立模块。

`ModelProvider` 不通过 CapabilityInvoker 执行。`harness-model` 的 ModelGateway 使用模型
原生协议，并只复用本模块的 `ProviderExecutionCoordinator` 作为 Retry/Fallback 数据面。

## 测试

```bash
.venv/bin/python -m pytest harness-runtime/tests -v
```
