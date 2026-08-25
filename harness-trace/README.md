# harness-trace

第二阶段在第一阶段 Request/Runtime/Capability Span 基础上增加 `PLAN`、
`PLAN_NODE` 和 `SCHEDULER` 类型，用于表达计划与节点的稳定执行层级。

`harness-trace` 提供与观测厂商无关的 Trace、Span 和 Event 抽象。Runtime 面向本模块的 `Tracer` 编程，不直接依赖 OpenTelemetry SDK。

## 公共 API

- `Tracer`：`start_span()`、`add_event()`、`end_span()` 抽象。
- `Span`、`TraceEvent`、`TraceError`：深度不可变的可序列化快照。
- `SpanStatus`：`RUNNING`、`OK`、`ERROR`、`CANCELLED`。
- `SpanType`：REQUEST、RUNTIME、POLICY、REGISTRY_RESOLVE、CAPABILITY、AGENT、TOOL。
- `InMemoryTracer`：线程安全地保存 Span/Event，支持按 trace/span 过滤和 TraceContext 转换。
- `ConsoleTracer`：在保留内存快照的同时输出 JSON Lines 生命周期记录。
- `TraceStateError`：Span 生命周期非法使用时抛出。

## Runtime Trace 层级

```text
REQUEST
└── RUNTIME
    ├── REGISTRY_RESOLVE
    ├── POLICY
    └── CAPABILITY
        └── AGENT / TOOL
```

已有 `InvocationContext.trace_context` 会作为新 REQUEST Span 的父上下文，实现同一进程内的 Trace 续接。`request.options.trace=false` 时 Runtime 不创建 Span，也不向结果强制写入 trace ID。

## 生命周期约束

- 所有时间必须带时区。
- RUNNING Span 不能有结束时间或错误。
- ERROR Span 必须携带错误；OK/CANCELLED Span 不能携带错误。
- 已结束 Span 不能再次结束、添加 Event 或创建子 Span。
- `HarnessError` 会归一化为包含类型、消息和错误码的 `TraceError`。

## 依赖边界

只依赖 `harness-contracts`。业务插件只接收 Runtime 传播的 TraceContext，无需维护 Harness 主链路。

## 测试

项目安装后运行：

```bash
.venv/bin/python -m unittest discover -s harness-trace/tests -v
```

## 阶段一非目标

不实现 OpenTelemetry SDK 绑定、Exporter、远程 Collector、采样、Metrics、日志聚合、跨进程 Baggage 管理或持久化 Trace Store。
