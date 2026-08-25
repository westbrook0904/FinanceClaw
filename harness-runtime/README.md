# harness-runtime

`harness-runtime` 负责一次 Invocation 的执行生命周期，是阶段一各核心模块之间的薄协调层。

## 职责

- 接收标准 `Request`
- 通过 `InvocationContextFactory` 构造可信、只读的 `InvocationContext`
- 建立阶段一 Trace 层级
- 通过 `CapabilityRegistry.resolve()` 解析目标能力
- 执行 `PRE_EXECUTE` Policy 链
- 根据 Provider 类型分别调用 `AgentSPI.invoke()` 或 `ToolSPI.execute()`
- 应用单次 Capability 调用超时
- 把拒绝、失败和成功统一归一化成 `ResultEnvelope`
- 在调用方取消 task 时关闭 Trace 并继续传播取消语义

阶段一实际执行顺序为：

```text
Request
  ↓
ContextFactory.create
  ↓
REQUEST / RUNTIME trace
  ↓
Registry.resolve
  ↓
PRE_EXECUTE Policy
  ↓
CAPABILITY
  └── AGENT / TOOL
  ↓
ResultEnvelope normalize
  ↓
Trace finish
```

Policy 放在 Registry 之后是有意的：阶段一只实现 `PRE_EXECUTE`，而当前
`PolicyContext` 需要已经解析出的 `CapabilityDescriptor`。阶段一不实现 `PRE_ROUTE`。

## Context 边界

默认 `DefaultInvocationContextFactory` **不会**把 Request 中的 `user_id` 或
`tenant_id` 直接当作可信 Identity/Tenant。认证和租户解析应在应用/Bootstrap 边界完成，
通过自定义 `InvocationContextFactory` 注入 Runtime。

如果 Request 设置了 `timeout_ms`，默认 Factory 同时计算 `deadline_at`；Runtime 在实际
Agent/Tool 调用处执行对应的 asyncio timeout。

## Trace 层级

Runtime 产生：

```text
REQUEST
└── RUNTIME
    ├── REGISTRY_RESOLVE
    ├── POLICY
    └── CAPABILITY
        └── AGENT / TOOL
```

`request.options.trace = false` 时 Runtime 不产生 Span，也不会给结果强制注入 trace id。

## 依赖

允许依赖：

- `harness-contracts`
- `harness-spi`
- `harness-registry`
- `harness-policy`
- `harness-trace`

Runtime 不依赖具体 Plugin 实现，也不负责本地插件发现和生命周期。

## 阶段一非目标

- Planner / LLM Router
- 多 Agent 编排
- Memory / RAG / Workflow
- SQL、金融指标、Prompt、数据源等业务逻辑
- PRE_ROUTE / POST_EXECUTE Policy
- Remote Agent / MCP / HTTP Provider
- OpenTelemetry 等具体 Trace 后端
- 数据库持久化
