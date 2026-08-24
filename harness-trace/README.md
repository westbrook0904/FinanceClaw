# harness-trace

## 职责

定义 Tracer、Span 和 Event 抽象，记录 Request、Policy、Registry、Capability、Result 与 Error 的调用层级。

## 依赖边界

- 只依赖 `harness-contracts` 与必要的 `harness-spi` 协议。
- Runtime 面向 Trace 抽象编程，不直接绑定 OpenTelemetry。
- Trace 负责观测，不改变业务执行结果。

## 阶段一非目标

只需要内存或控制台实现，不集成 OpenTelemetry 后端、Metrics 平台或分布式导出。
