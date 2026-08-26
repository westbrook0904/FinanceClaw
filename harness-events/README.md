# harness-events

`harness-events` 是阶段二最小进程内执行事件边界。ExecutionEngine 只发布业务无关的
Plan / Node / Approval / Async / Checkpoint 事件，未来 Metrics、Audit、Billing、UI
通过 `EventSubscriber` 订阅，不反向耦合 ExecutionEngine。

提供：

- `ExecutionEvent` / `ExecutionEventName`
- `EventPublisher` / `EventSubscriber`
- `InMemoryEventBus`
- `NoOpEventPublisher`

阶段二不连接 Kafka、Redis Stream、NATS 或外部 Event Broker。StateStore 仍是恢复执行
的事实来源；事件用于观察和集成，不替代 checkpoint。
