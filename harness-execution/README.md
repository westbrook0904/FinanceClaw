# harness-execution

`harness-execution` 负责执行已经通过 `PlanValidator` 的 `ExecutionPlan`。Basic
Scheduler 支持串行、并行、Join、条件分支、结构化输入/输出 Binding、跳过传播、
fail-fast、continue-on-failure、Plan 级并发限制、Retry、Deadline 与 Cancellation。

所有 Capability 节点都通过 `CapabilityInvoker` 执行，Scheduler 不直接访问 Registry
或 Provider。Retry 只有在错误可重试、Capability 副作用/幂等规则允许且 Deadline
仍有剩余时发生；所有尝试共享 Request、Plan 和 Node 合并后的绝对 Deadline。

`ExecutionEngine.cancel(plan_id, reason)` 使用进程内 `CancellationSignal` 停止新节点、
取消运行中 Task，并把剩余节点与 Plan 收敛到 `CANCELLED`。当前状态仍暂存在
ExecutionEngine 内存中；Checkpoint/StateStore 和外部 Resume 按后续里程碑实现。
