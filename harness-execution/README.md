# harness-execution

`harness-execution` 负责验证、执行和恢复调用方提供的 `ExecutionPlan`。它实现 Stage 2
可靠执行状态机和 Stage 3A Provider-safe checkpoint/resume，不包含业务逻辑；所有 Capability Node 都通过
`CapabilityInvoker`，Scheduler 不直接访问 Registry Provider。

## 公共 API

- `ExecutionEngine.execute(request, plan)`：创建、校验并推进 Plan，直到终态或明确
  WAITING。
- `ExecutionEngine.resume(plan_id)`：从 StateStore 加载同一快照并继续。
- `ExecutionEngine.resolve_approval(plan_id, decision)`：持久化审批决定并继续。
- `ExecutionEngine.complete_async_node(plan_id, node_id, terminal_result)`：提交异步
  Capability 终态并继续。
- `ExecutionEngine.cancel(plan_id, reason)`：取消当前进程内活动 Plan。
- `ExecutionEngine.state(plan_id)`：读取当前 Engine 已见过的 State 快照。
- `BasicScheduler`：DAG、Binding、Condition、Node Deadline、Cancellation 和结果组合；
  Provider Retry/Fallback 委托给 Runtime。
- `CancellationSignal`：进程内可变取消信号及可持久化 CancellationContext 快照。
- `InputResolver`、`ConditionEvaluator`、`resolve_json_pointer()`：结构化数据求值。

## 调度语义

BasicScheduler 支持：

- 串行、多个并行 root 和 Plan 级 `max_concurrency`；
- Join 等待全部前驱终态后，再判断至少一条入边是否激活；
- `SUCCESS/FAILED/DENIED/COMPLETED/ALWAYS` Edge Trigger；
- 受限 Condition 表达式和 JSON Pointer；
- Request、literal、Node Result Input Binding 与最终 Output Binding；
- 非活动分支递归收敛为 `SKIPPED`；
- Node `FAIL_PLAN` / `CONTINUE` 与 Plan `FAIL_FAST`；
- 有完整输出但存在可继续问题时组合 `PARTIAL`。

Capability 返回 `PARTIAL` 时节点仍视为成功并可供下游读取 output，issues 会提升到 Plan；
返回 `ACCEPTED` 时节点进入 WAITING，不占用长期 asyncio Task。

## Retry / Fallback、Deadline 与取消

Scheduler 每个 Node 只调用一次 `CapabilityInvoker`。Runtime 在已选 Provider 内执行
Retry，因此 Retry 不会重新 Selection；当前 Provider 的 Retry 耗尽后，只有最终错误
`fallbackable=true` 才会从尚未尝试的 eligible Provider 中重新选择。

Retry 只有同时满足以下条件才会发生：

1. 最终结果是 FAILED，且 `ErrorDetail.retryable=true`；
2. 尚未达到 `RetryPolicy.max_attempts`；
3. Capability 是 `NONE/READ` 副作用，或 WRITE 声明支持幂等且 Node 提供
   `idempotency_key`；
4. 绝对 Deadline 仍容纳下一次尝试和退避。

退避采用无 jitter 的确定性指数策略。Request、Plan 和 Node 三层时间预算合并为最早的
绝对 Deadline，全部 Retry 共享该时间点。

`NONE/READ` 允许跨 Provider Fallback。WRITE 默认 fail-closed，只有 Capability 声明
支持幂等、Node 提供稳定 `idempotency_key`，且 source/target Provider 的非空
`equivalence_group` 完全相同，才允许切换；否则返回
`HARNESS.PROVIDER.FALLBACK_UNSAFE`。

`ExecutionEngine.cancel()` 使用 `CancellationSignal` 停止新节点、取消运行 Task，并
把剩余节点和 Plan 收敛到 CANCELLED。调用 `execute()` 的客户端 task 被取消时，
`CancelledError` 仍向上传播，同时开放 Span 和节点状态被清理。

## Checkpoint 与 Resume

ExecutionEngine 通过 StateStore 在 Plan 创建、Provider 选择完成、每次 Provider 调用前、
Provider attempt 完成、Retry/Fallback 切换、节点终态、WAITING、取消和 Plan 终态等稳定
边界保存完整 `PlanExecutionRecord`。Provider 调用前 checkpoint 失败会阻止 Provider
产生副作用。

`NodeExecutionState` 保存当前 `selected_provider_id`、Provider/retry 二维 attempt、
selection key、equivalence group、attempt history 和最近一次 Provider 结果。因而进程即使
在 Provider 返回后、节点终态落盘前退出，也能复用已完成 attempt 而不会重复调用。

`resume(plan_id)` 复用持久化的 Plan、InvocationContext 和 Node State：

- 已完成节点直接复用已保存的 `ResultEnvelope`；
- 已选择 Provider 的 `NONE/READ` 节点优先重放原 Provider，失败后才受控 fallback；
- 中断的 WRITE 只有声明 `OPTIONAL/REQUIRED` 幂等且 Node 有 key 才固定重放原 Provider，
  否则返回 `HARNESS.PLAN.RESUME_UNSAFE`；
- WRITE 重放失败后仍必须满足稳定 key 和相同非空 equivalence group 才能 fallback；
- checkpointed Provider 缺失或 equivalence group 发生变化时返回
  `HARNESS.PROVIDER.RESUME_UNSAFE`，不会重新自由 Selection；
- attempt 从持久化值继续，不从 1 重置；
- Request/Plan Deadline 与已开始 Node timeout 都不会被延长；
- WAITING 在没有外部终态时幂等返回原 Continuation；
- 缺失、损坏或不可读状态 fail-closed，并返回结构化错误。

跨进程恢复需要文件型 `SQLiteStateStore`；默认 InMemoryStateStore 只适合单进程。

## Human Approval

显式 `PlanNodeKind.APPROVAL` 是一等等待节点：

1. Scheduler 将节点推进到 WAITING；
2. ApprovalCoordinator 生成安全、可持久化的 `ApprovalRequest`；
3. API 返回带稳定 `plan_id/node_id/approval_id` 的 ACCEPTED；
4. 外部提交 `ApprovalDecision`；
5. APPROVED 映射为节点 SUCCEEDED，REJECTED 映射为 DENIED；
6. 决策先 checkpoint，再复用 Resume 继续 DAG。

拒绝仍遵守 Node FailurePolicy 和 DENIED Edge，可进入显式拒绝/补偿分支。ApprovalRequest
只读取审批专用摘要字段，不复制任意 metadata、完整输入、Prompt 或 Secret。

PRE_EXECUTE Policy 的 REQUIRE_APPROVAL 复用同一流程。批准后生成持久化
`ApprovalGrant`，恢复时再次进入 Policy，而不是绕过治理边界。

## Async WAITING

Capability 可以返回 `ACCEPTED + Continuation.job_ref`：

1. 节点进入 WAITING，job 信息持久化到 `pending_jobs`；
2. API 立即返回 ACCEPTED；
3. 外部调用 `complete_async_node()` 提交终态；
4. 终态只能是 SUCCESS、PARTIAL、FAILED、DENIED 或 CANCELLED；
5. completion 先 checkpoint，再 Resume。

Provider 可以省略 Continuation 的 plan/node ID，由 Engine 补全，但 `job_ref` 必须存在；
显式冲突的引用、重复 completion 或再次提交 ACCEPTED 都会 fail-closed。WAITING
checkpoint 与 Approval/Job materialization 之间的崩溃窗口可在 Resume 时修复。

## Policy、Trace 与 Events

PRE_PLAN 在 Scheduler 启动前运行；PRE_EXECUTE 由每次 Invoker 尝试执行。Plan Trace
包含 REQUEST → RUNTIME → PLAN → SCHEDULER/PLAN_NODE → Capability 子树。

ExecutionEngine 同时发布 Plan/Node/Approval/Async/Checkpoint Execution Events。事件是
best-effort 观察面，Publisher/Subscriber 失败不会覆盖 StateStore 中的执行事实。

## 当前范围

当前不实现动态 Plan Patch、分布式 Scheduler/锁、多 Scheduler 竞争、外部 callback
server、轮询框架或外部 Event Broker。ModelProvider 由独立 ModelGateway 调用，不作为
DAG Agent/Tool Node 直接执行。

## 测试

```bash
.venv/bin/python -m pytest harness-execution/tests -v
```
