# harness-runtime

`harness-runtime` 提供 Request 级 Direct Invocation 生命周期，以及供 Direct Runtime
和后续 ExecutionEngine 共同使用的 `CapabilityInvoker` 受控调用边界。

`CapabilityInvoker` 统一执行 Registry resolve、PRE_EXECUTE Policy、Capability Trace、
Agent/Tool 调用、绝对 deadline/timeout、asyncio cancellation、错误和 ResultEnvelope
归一化。调用方
不得绕过它直接从 Registry 取得 Provider 后执行。

`harness-runtime` 是阶段一核心模块之间的薄协调层，负责一次 Request 的完整 Invocation 生命周期，不包含插件发现、业务路由或具体业务逻辑。

## 公共 API

- `HarnessRuntime.invoke(Request) -> ResultEnvelope`
- `CapabilityInvoker.invoke(capability_id, input, context, ...) -> ResultEnvelope`
- `InvocationLifecycle`：共享 Context 创建、Trace 传播、结果与 Span 收尾语义。
- `InvocationContextFactory.create(Request) -> InvocationContext`
- `DefaultInvocationContextFactory`：构造最小可信 Context，并按 `timeout_ms` 计算 deadline。

## 执行顺序

```text
Request
  ↓
ContextFactory.create
  ↓
REQUEST / RUNTIME trace
  ↓
Registry.resolve(CapabilityQuery)
  ↓
PRE_EXECUTE PolicyEngine
  ↓
CAPABILITY
  └── AgentSPI.invoke / ToolSPI.execute
  ↓
ResultEnvelope normalize
  ↓
finish Trace
```

Policy 位于 Registry 之后，因为阶段一 PolicyContext 需要已解析的 CapabilityDescriptor。RequestTarget 中可选的 `plugin` 会作为 Provider 来源限定条件参与 Registry 查询。

## Agent 与 Tool 适配

- Agent 接收 `AgentRequest(input=request.input)`。
- Tool 要求 `RequestInput.content` 是 JSON object，并转换为 `ToolRequest(arguments=content)`。
- Provider SPI 类型必须与 Descriptor 的 `CapabilityType` 一致。
- Provider 必须返回 `ResultEnvelope`，否则归一化为 Capability failure。

## Context 与安全边界

默认 Factory 不把 Request 中的 `user_id`、`tenant_id` 直接提升为可信 IdentityContext/TenantContext。认证和租户解析应在应用边界完成，并通过自定义 `InvocationContextFactory` 注入。

## 超时、取消和错误

- 相对 `timeout_ms` 和绝对 `deadline_at` 取最早值，通过 `asyncio.timeout()` 应用于
  实际 Provider 调用。
- 调用方取消 task 时，Runtime 关闭开放 Span 并继续传播 `CancelledError`。
- Request、Registry、Policy、Capability 和 Timeout 异常统一转换为 `ResultEnvelope.failure()`。
- Policy 拒绝转换为 `ResultEnvelope.denied()`，不会调用 Provider。
- Trace 开启时，最终结果统一携带 REQUEST trace ID。

## 依赖边界

允许依赖 Contracts、SPI、Registry、Policy 和 Trace。Runtime 不依赖具体插件，也不负责 Plugin 生命周期。

## 测试

项目安装后运行：

```bash
.venv/bin/python -m unittest discover -s harness-runtime/tests -v
```

## 当前非目标

本模块暂不实现 Planner、DAG Scheduler、Checkpoint/Resume、远程 Provider
或数据持久化；这些能力按第二阶段后续里程碑独立实现。
