# harness-state

`harness-state` 提供业务状态机无关的 `StateStore` SPI，以及默认
`InMemoryStateStore` 和持久化 `SQLiteStateStore`。

SQLite 使用 `plan_id/state_version/payload_json/created_at/updated_at` 单表结构，
完整 DAG 运行记录以 JSON Snapshot 原子保存。模块不解释 READY、RUNNING、Retry
或 Cancellation 等状态迁移，也不提前把 Node State 拆成关系表。

```python
from harness_state import SQLiteStateStore

store = SQLiteStateStore("financeclaw-state.db")
record = await store.load("plan-123")
```

默认 Bootstrap 使用 `InMemoryStateStore`，因此 `build_harness()` 不产生磁盘副作用。
Resume、Approval 和异步节点恢复由后续 ExecutionEngine 里程碑实现。
