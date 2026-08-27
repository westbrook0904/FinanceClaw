# harness-state

`harness-state` 提供业务状态机无关的 `StateStore` SPI，以及
`InMemoryStateStore` 和持久化 `SQLiteStateStore`。StateStore 只原子保存完整
`PlanExecutionRecord`，不解释 DAG 状态迁移。

## 公共 API

- `StateStore.create(record)`：按 `plan_id` 原子创建，重复创建抛出
  `StateRecordExistsError`。
- `StateStore.load(plan_id)`：返回分离的记录快照；不存在时返回 `None`。
- `StateStore.save(record)`：原子替换已有快照；记录不存在时抛出
  `StateRecordNotFoundError`。
- `StateStore.delete(plan_id)`：幂等删除。
- `StateStoreError`：存储、序列化或损坏数据的统一失败边界。

## 实现

`InMemoryStateStore` 用于默认组装和单进程测试。所有写入与读取都经过 JSON
round-trip，调用方不能通过原对象引用修改已保存状态。

`SQLiteStateStore` 使用单表：

```text
plan_id / state_version / payload_json / created_at / updated_at
```

完整 Plan、可恢复 InvocationContext 和 Plan/Node State 以 JSON Snapshot 保存。每次
操作使用短生命周期连接，并通过 `asyncio.to_thread()` 避免阻塞事件循环；文件数据库
可在 Application/Engine 重建后继续加载，`:memory:` 模式通过 keeper connection 保持
同一 Store 实例内的数据。

```python
from harness_state import SQLiteStateStore

store = SQLiteStateStore("financeclaw-state.db")
record = await store.load("plan-123")
```

## 与 ExecutionEngine 的关系

ExecutionEngine 在 Plan 创建、节点调用前、Retry、节点终态、WAITING、取消和 Plan
终态等稳定边界 checkpoint。Resume、Approval 决策和 Async completion 都先更新
StateStore，再从相同恢复状态机继续 DAG。

默认 `build_harness()` 使用 `InMemoryStateStore`，不会创建数据库文件；跨进程恢复
必须显式注入文件型 `SQLiteStateStore`。

## 当前边界

SQLite 是第二阶段单进程、单 writer 参考实现，不提供 CAS、lease、分布式锁、多
Scheduler 竞争或数据库故障转移。State 中保留 `state_version`，为后续并发控制演进
预留协议位置。

## 测试

```bash
.venv/bin/python -m pytest harness-state/tests -v
```
