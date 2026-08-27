# harness-events

`harness-events` 是第二阶段最小进程内执行事件边界。ExecutionEngine 发布业务无关的
Plan、Node、Provider、Approval、Async 和 Checkpoint 事实，未来 Metrics、Audit、Billing 或 UI
可以通过 `EventSubscriber` 订阅，而无需反向耦合执行状态机。

## 公共 API

- `ExecutionEvent`：包含 event ID、名称、request ID、可选 plan/node ID、时间、
  state version、trace ID 和不可变 attributes；request/plan 至少存在一个。
- `ExecutionEventName`：稳定事件名称枚举。
- `EventPublisher.publish(event)`：异步发布接口。
- `EventSubscriber.on_event(event)`：异步订阅接口。
- `InMemoryEventBus`：按订阅顺序同步分发，并保留事件快照。
- `NoOpEventPublisher`：校验事件后丢弃的无副作用实现。

## 事件集合

```text
plan.created / started / waiting / resumed / completed / failed / cancelled
node.ready / started / retrying / waiting / resumed / completed / failed / denied / cancelled
approval.requested / approval.resolved
async.accepted / async.completed
checkpoint.saved
provider.candidates / selected / retrying / fallback / failed
```

`InMemoryEventBus.subscribe()` 幂等添加订阅者，`unsubscribe()` 幂等移除；
`events()` 返回不可变事件快照。

## 执行语义

ExecutionEngine 从相邻 checkpoint 状态推导大部分 Plan/Node 事件，并显式发布
Approval 和 Async ingress 事件；CapabilityInvoker 发布 Provider 执行事件，因此
Direct Invocation 与 Plan Node 使用相同观测语义。事件发布由执行层按 best-effort 处理：Publisher 或
Subscriber 异常不会覆盖已经保存的 StateStore 事实，也不会使 DAG 执行失败。

StateStore 是恢复事实来源；Events 只用于观察和集成，不替代 checkpoint，也不保证
外部持久投递。

## 当前边界

本模块只依赖 `harness-contracts`，不连接 Kafka、Redis Stream、NATS 或其他外部
Broker，不实现重放、消费位点、持久订阅或 exactly-once。

## 测试

```bash
.venv/bin/python -m pytest harness-events/tests -v
```
