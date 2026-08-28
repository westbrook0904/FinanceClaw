# harness-trace

`harness-trace` 提供与观测厂商无关的 Trace、Span 和瞬时 Event 抽象。第二阶段在
Request/Runtime/Capability 层级上增加 Plan、Scheduler 和 Plan Node，使完整 DAG 调用链
可审计。

## 公共 API

- `Tracer`：`start_span()`、`add_event()`、`end_span()` 抽象。
- `Span`、`TraceEvent`、`TraceError`：深度不可变、可序列化快照。
- `SpanStatus`：`RUNNING`、`OK`、`ERROR`、`CANCELLED`。
- `SpanType`：`REQUEST`、`RUNTIME`、`POLICY`、`REGISTRY_RESOLVE`、`PROVIDER_SELECT`、
  `CAPABILITY`、`MODEL`、`AGENT`、`TOOL`、`PLAN`、`SCHEDULER`、
  `PLAN_NODE`、`ROUTE`、`PLANNER`。
- `InMemoryTracer`：线程安全保存 Span/Event，可按 trace/span 过滤并生成
  `TraceContext`。
- `ConsoleTracer`：保留内存快照，同时输出 JSON Lines 生命周期记录。
- `TraceStateError`：非法 Span 生命周期操作。

## Trace 层级

Direct Invocation：

```text
REQUEST
└── RUNTIME
    ├── REGISTRY_RESOLVE
    ├── PROVIDER_SELECT
    ├── POLICY
    └── CAPABILITY
        └── AGENT / TOOL
```

Plan Execution：

```text
REQUEST
└── RUNTIME
    └── PLAN
        ├── POLICY（PRE_PLAN）
        ├── SCHEDULER
        └── PLAN_NODE（与 SCHEDULER 同级）
            ├── REGISTRY_RESOLVE
            ├── PROVIDER_SELECT
            ├── POLICY
            └── CAPABILITY
                └── AGENT / TOOL
```

ModelGateway：

```text
REGISTRY_RESOLVE
├── PROVIDER_SELECT（initial / fallback）
└── MODEL（每次 Provider attempt）
```

统一 handle：

```text
REQUEST
└── RUNTIME
    ├── ROUTE
    │   └── MODEL（LLMRouter，可选）
    ├── PLANNER
    │   └── MODEL（LLMPlanner，每次 generation）
    └── PLAN → SCHEDULER / PLAN_NODE / Provider
```

每次初始 Provider 选择和 fallback 选择产生一个短生命周期 `PROVIDER_SELECT` Span；
候选集、retry、fallback 和失败细节同时记录为 Trace Event。WAITING、Resume、Approval
和 Checkpoint 等瞬时变化继续使用 Event/Attribute。已有 `InvocationContext.trace_context` 可续接上游 trace；
`request.options.trace=false` 时不创建 Span，也不强制写入 result trace ID。

ROUTE/PLANNER attributes 只保存安全标识符、计数和稳定 hash。RequestSummary、Catalog 原文、
Prompt、模型响应、credential 与隐藏推理不会进入 Trace；repair 记录为 PLANNER 上的 Trace Event，
不新增瞬时 SpanType。

## 生命周期约束

- 所有时间必须包含时区。
- RUNNING Span 不能有结束时间或错误。
- ERROR Span 必须携带错误；OK/CANCELLED Span 不能携带错误。
- 已结束 Span 不能再次结束、添加 Event 或创建子 Span。
- `HarnessError` 会归一化为包含类型、摘要和错误码的 `TraceError`；MODEL Span 使用固定错误
  摘要，禁止复制 Provider/模型返回的原始错误消息。
- 并行 Plan Node 共享 Plan/Scheduler 上下文，但各自保留独立 PLAN_NODE 子树。

## Trace 与 Execution Events

Trace Event 绑定具体 Span，用于调用链内诊断；`harness-events` 的 ExecutionEvent 是
独立的业务无关执行事实，可供订阅者观察。两者都不是 StateStore，也不承担 Resume。

## 依赖边界与当前范围

本模块只依赖 `harness-contracts`。业务插件只接收传播后的 TraceContext，无需维护主
链路。OpenTelemetry SDK/Exporter、远程 Collector、采样、Metrics、日志聚合和持久化
Trace Store 尚未实现。

## 测试

```bash
.venv/bin/python -m pytest harness-trace/tests -v
```
