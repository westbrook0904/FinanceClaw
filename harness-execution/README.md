# harness-execution

`harness-execution` 负责执行已经通过 `PlanValidator` 的 `ExecutionPlan`。Basic
Scheduler 支持串行、并行、Join、条件分支、结构化输入/输出 Binding、跳过传播、
fail-fast、continue-on-failure、Plan 级并发限制、Retry、Deadline 与 Cancellation。

所有 Capability 节点都通过 `CapabilityInvoker` 执行，Scheduler 不直接访问 Registry
或 Provider。Retry 只有在错误可重试、Capability 副作用/幂等规则允许且 Deadline
仍有剩余时发生；所有尝试共享 Request、Plan 和 Node 合并后的绝对 Deadline。

`ExecutionEngine.cancel(plan_id, reason)` 使用进程内 `CancellationSignal` 停止新节点、
取消运行中 Task，并把剩余节点与 Plan 收敛到 `CANCELLED`。ExecutionEngine 通过
`StateStore` 在 Plan 创建、节点调用前、节点终态、取消和 Plan 终态等稳定边界保存
完整 JSON Snapshot。

## Resume

`ExecutionEngine.resume(plan_id)` 从 `StateStore` 加载同一个 Plan 的
`PlanExecutionRecord`，复用持久化的 `ExecutionPlan`、`InvocationContext` 和节点状态
继续推进，不重新创建 Request Context，也不会重新执行已经完成的节点。

恢复规则保持原执行语义：

- `SUCCEEDED` / `FAILED` / `DENIED` 等已完成节点直接复用已保存的 `ResultEnvelope`；
- 中断在 `RUNNING` 的 `NONE` / `READ` Capability 可以安全重放；
- 中断在 `RUNNING` 的 `WRITE` Capability 只有声明支持幂等且节点提供
  `idempotency_key` 时才重放，否则返回 `HARNESS.PLAN.RESUME_UNSAFE`；
- Retry 从持久化 `attempt` 继续，不重新从 1 开始；
- Request/Plan 的绝对 Deadline 不会因 Resume 延长；已开始节点的 `timeout_ms` 仍从
  原 `started_at` 计算；
- `WAITING` 状态在尚无外部完成事件时幂等返回原 Continuation；后续 Approval/Async
  里程碑可以先更新等待节点，再复用同一 Resume 入口继续调度；
- 缺失或不可读状态分别返回 `HARNESS.PLAN.NOT_FOUND` 与
  `HARNESS.PLAN.STATE_LOAD_FAILED`。

跨进程恢复需要使用 SQLite 等持久化 `StateStore`；默认 `InMemoryStateStore` 只适合
单进程测试和默认组装。

## Human Approval

显式 `PlanNodeKind.APPROVAL` 是 Execution Engine 的一等等待节点，不注册为
Capability，也不会长期占用 asyncio Task。节点到达后：

1. Scheduler 先把节点推进到 `WAITING`；
2. `ApprovalCoordinator` 生成持久化 `ApprovalRequest` 并写入
   `PlanExecutionState.pending_approvals`；
3. 当前 API 返回 `ResultStatus.ACCEPTED`，Continuation 同时携带 `plan_id`、`node_id`
   与稳定 `approval_id`；
4. 外部调用 `ExecutionEngine.resolve_approval(plan_id, decision)`；
5. `APPROVED` 映射为 Approval Node `SUCCEEDED`，`REJECTED` 映射为 `DENIED`；
6. 决策后的快照先写回 StateStore，再复用 `resume(plan_id)` 继续 DAG。

审批拒绝仍遵守 Node `failure_policy` 和普通 `EdgeTrigger.DENIED` 语义，因此可以选择
fail-plan，也可以通过 `CONTINUE + DENIED edge` 进入补偿/拒绝分支。

`ApprovalRequest` 不复制任意节点 metadata 或完整请求输入。显式 Approval Node 只读取
以下专用安全摘要字段：

- `metadata.approval_reason`
- `metadata.approval_resource_category`
- `metadata.approval_parameter_names`

其他 metadata（包括 Secret、Token 或完整 Prompt）不会进入 ApprovalRequest。

Policy 触发的 `REQUIRE_APPROVAL` 将在第二阶段第 10 步 Policy 完整化时接入；第 8 步
先稳定共享的 ApprovalRequest / ApprovalDecision / WAITING / checkpoint / resume 基础链路。
