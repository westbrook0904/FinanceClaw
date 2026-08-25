# harness-trace

## 职责

提供与具体观测厂商无关的 Trace / Span / Event 抽象，使 Runtime 能统一创建完整调用链，
而不直接依赖 OpenTelemetry 或其他 SDK。

## 依赖边界

- 只依赖 `harness-contracts`。
- Runtime 面向 `Tracer` SPI 编程，不直接调用 OpenTelemetry API。
- 业务插件无需维护 Harness 主链路，只接收 Runtime 传播的 `TraceContext`。

## 阶段一实现

- `Tracer` SPI：`start_span()` / `add_event()` / `end_span()`。
- `Span` / `TraceEvent` 不可变快照与 `SpanStatus` / `SpanType`。
- `InMemoryTracer`：线程安全记录 Span 与 Event，便于 Runtime 和测试使用。
- `ConsoleTracer`：以 JSON Lines 输出完整生命周期，同时保留内存快照。
- 父子 Span 与已有 `TraceContext` 续接。
- 阶段一标准 Span 类型：`REQUEST`、`RUNTIME`、`POLICY`、`REGISTRY_RESOLVE`、
  `CAPABILITY`、`AGENT`、`TOOL`。

典型层级：

```text
REQUEST
└── RUNTIME
    ├── POLICY
    ├── REGISTRY_RESOLVE
    └── CAPABILITY
        └── TOOL / AGENT
```

## 阶段一非目标

不实现 OpenTelemetry SDK 绑定、Exporter、远程 Collector、采样、Metrics、日志聚合、
跨进程 Baggage 管理或持久化 Trace Store。后续可通过新的 `Tracer` 实现接入 OTel，
不改变 Runtime 的调用方式。
