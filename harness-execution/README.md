# harness-execution

`harness-execution` 负责执行已经通过 `PlanValidator` 的 `ExecutionPlan`。Basic
Scheduler 支持串行、并行、Join、条件分支、结构化输入/输出 Binding、跳过传播、
fail-fast、continue-on-failure 和 Plan 级并发限制。

所有 Capability 节点都通过 `CapabilityInvoker` 执行，Scheduler 不直接访问 Registry
或 Provider。当前状态暂存在 ExecutionEngine 内存中；Checkpoint/StateStore、Retry、
Deadline 和外部 Resume 按后续里程碑实现。
