# harness-contracts

`harness-contracts` 是所有 Harness 模块共享的稳定、业务无关协议层，不依赖其他
Harness 模块或插件。Stage 2 在 Request/Context/Capability/Result 基线上补齐 Plan、
可恢复状态、审批和异步 Continuation；Stage 3A 进一步冻结 Provider 身份、Health、
Selection、ProviderAttempt 和安全恢复协议；Stage 3B 冻结请求级 ExecutionMode 与
RouteDecision 协议；Stage 3C Step 2 下沉 provider-neutral 模型 usage 遥测、strict structured
output 与 generation reservation/receipt 契约，避免 `harness-contracts` 反向依赖模型实现层。
Agent Foundation F2 新增 `ContextItem`、`ContextSnapshot`、`ContextProjection`、
`ContextUseRecord` 及固定来源、信任、敏感级别与 omission reason 枚举。
Agent Foundation F3 新增 `MemorySubjectScope`、`MemoryRecord/Query/Slice`、模型安全的
`MemoryWriteDraft`、可信 `MemoryWriteProposal` 及 Memory 错误分类。
Agent Foundation F4a 新增兼容默认 `UNKNOWN` 的 `CapabilityCompletionMode`、
`ExplorationBudget/Profile/ProfileSnapshot/TurnDraft/Action/Observation/State`、typed
`ExplorationNodeSpec` 与 `PlanExecutionState.explorations`。

Agent Foundation 重基线将未发布的 `NormalizedCost*`、token/cost reservation 上界与相关
ModelAttemptPolicy 字段作为 pre-release corrective break 删除；仓库没有旧 reservation 的生产
持久化消费者，不提供 wire migration。旧快照必须丢弃并重新 prepare。

## 公共 API

| 分类 | 类型 |
|---|---|
| 请求 | `Request`、`RequestInput`、`RequestTarget`、`RequestOptions` |
| 路由 | `ExecutionMode`、`RouteType`、`RouteSource`、`RouteDecision` |
| 计划 | `ExecutionPlan`、`PlanNode`、`ExplorationNodeSpec`、`PlanEdge`、Binding、Condition、Budget、Retry / Failure Policy |
| 执行状态 | `PlanExecutionState`、`NodeExecutionState`、`ExplorationState`、Action/Observation 及状态枚举 |
| 持久化 | `PlanExecutionRecord`，包含 Plan、可恢复 Context 和完整 State |
| 调用上下文 | `InvocationContext`、Identity、Tenant、Trace、Cancellation Context |
| 模型 Context | `ContextItem`、`ContextSnapshot`、`ContextProjection`、`ContextUseRecord`、`ContextProjectionLimits` |
| 长期 Memory | `MemorySubjectScope`、`MemoryRecord`、`MemoryQuery`、`MemorySlice`、`MemoryWriteDraft/Proposal` |
| 能力 | `CapabilityDescriptor`、`CapabilityType`、`CapabilityExecutionProfile`、`CapabilityCompletionMode` |
| 最小探索 | `ExplorationProfile/Snapshot`、`ExplorationBudget/Usage`、`ExplorationTurnDraft`、`ActionProposal`、`Observation` |
| Provider Fabric | `ProviderDescriptor`、`ProviderHealthSnapshot`、`ProviderAttempt`、`ProviderPin`、Selection 契约 |
| 模型生成 | `StructuredOutputSpec`、`ModelProviderFeatures`、`ModelUsage`、`ModelGenerationAccounting`、`ModelGenerationReservation`、`ModelReservationReceipt` |
| 审批 | `ApprovalRequest`、`ApprovalDecision`、`ApprovalGrant` |
| 结果 | `ResultEnvelope`、`ResultOutput`、`ResultIssue`、`Continuation` |
| 错误 | `ErrorDetail`、`ErrorCode`、`HarnessError` 及模块异常 |
| 基础类型 | `ContractModel`、`MutableContractModel`、JSON 类型别名 |

公共类型应从 `harness_contracts` 顶层导入：

```python
from harness_contracts import Request, RequestInput, RequestTarget

request = Request(
    input=RequestInput(type="text", content="hello"),
    target=RequestTarget(capability="echo.reply/v1"),
)
```

## ExecutionPlan

`ExecutionPlan` 是不可变 DAG 定义。Capability Node 通过 Registry 调用 Agent/Tool；
Approval Node 是 ExecutionEngine 原生等待点，不注册为 Capability。F4a 还冻结了 Harness-owned
`EXPLORATION` node：它必须携带完整 `ExplorationNodeSpec/ProfileSnapshot`，与 capability、普通
input mapping、idempotency 和自由 metadata 互斥，Scheduler retry 固定为一次。

- Input Binding 显式读取 literal、原始 Request 或上游 `ResultEnvelope`。
- Output Binding 显式从节点结果组合最终 Plan 输出。
- Condition 使用 `eq/ne/lt/lte/gt/gte/exists/in/and/or/not` 白名单，不执行
  Python 表达式。
- Edge Trigger 支持 `SUCCESS`、`FAILED`、`DENIED`、`COMPLETED` 和
  `ALWAYS`。
- `PlanBudget` 当前只执行 Deadline 和最大并发；历史 token/cost 字段不参与 enforcement，也不
  进入 Agent Foundation 一期能力。
- Retry 总尝试次数、指数退避、节点超时、失败传播和幂等键都属于显式 Plan 契约。

单模型内可以确定的约束由 Pydantic 校验；环、跨节点引用、可达性和 Capability 是否存在
由 `harness-planning` 的 `PlanValidator` 校验。

## 状态与结果

Plan 状态包含 `CREATED/RUNNING/WAITING/SUCCEEDED/PARTIAL/FAILED/DENIED/CANCELLED`；
Node 额外包含 `PENDING/READY/SKIPPED`。Plan/Node State 是明确可变的运行快照，
其余跨模块协议默认深度冻结。

Capability Node 状态同时记录选中的 Provider、Provider/retry 二维 attempt、selection key、
equivalence group、Provider attempt history 和最近结果，用于跨进程恢复时固定重放原
Provider，禁止重新自由选择可能改变 WRITE 副作用目标的实现。

`ResultStatus` 支持：

- `SUCCESS`：必须有最终 output。
- `PARTIAL`：必须有 output 和至少一个 issue。
- `FAILED` / `DENIED`：必须有结构化 error。
- `CANCELLED`：无最终 output，可选 error。
- `ACCEPTED`：必须有可定位的 `Continuation`，用于 Approval 或异步等待。

`PlanExecutionRecord` 原子保存 `ExecutionPlan + InvocationContext +
PlanExecutionState`，并校验三者的 `plan_id/revision` 一致性。

当 Plan 含 `EXPLORATION` node 时，`PlanExecutionRecord` 还校验 map key、plan/node/exploration
identity、ProfileSnapshot，以及 inner Exploration 与 outer Node/Plan 的 status、result 和
`completed_at` 一致性。Profile/scope/action/result canonical hash 的重验位于
`harness-agentic`；Execution loop 在 F4b 前保持不可用。

`plan_id` 是一次 fresh execution lineage 的稳定身份，resume 保持不变；`revision` 是同一
execution 内的 Plan 定义版本，fresh execution 从 1 开始。未来 Workflow 定义使用独立的
`workflow_id/workflow_version`，不能复用 `plan_id`。

## 协议约束

- Pydantic 模型默认 `extra="forbid"`，拒绝未协商字段。
- Request、Context、Descriptor、Plan、Result 和 Approval 协议深度冻结。
- Invocation、Plan 与 Node State 明确可变。
- 时间字段必须包含时区。
- `Request.target` 对 Plan 请求可为空；Direct Invocation 仍由 Runtime 要求 target。
- 旧 Request 未指定 `execution_mode` 时默认为 `AUTO`；最终 `RouteDecision` 不允许保留
  `AUTO`，且模式、路由类型与目标字段必须形成合法组合。
- Request 中的 `user_id`、`tenant_id` 不能直接视为可信 Identity/Tenant。
- Context hash 使用 64 位小写 SHA-256；时间必须含时区，Projection included/omitted item ID
  必须唯一且互斥。
- Memory hash 使用 64 位小写 SHA-256；Record 是 create-only，Proposal provenance/evidence 必须
  一致，Draft 不含可信身份或存储控制字段。
- Capability 执行画像用 `side_effect`、`egress`、`idempotency` 支持重试、
  恢复与审批判断；`completion_mode` 默认 UNKNOWN，只有显式 SYNC 才具备 Minimal Explore
  eligibility，既有 FAST/PLAN 不受影响。
- `CapabilityType.MODEL` 只表达 Registry 中的稳定模型能力语义；模型原生生成协议位于
  `harness-model`，不进入 Agent/Tool 调用协议。
- `HarnessError.to_detail()` 把内部异常转换为可安全传播的结构化错误。

## 依赖边界

本模块不依赖任何其他 Harness 模块或业务插件，也不包含 Finance、SQL、RAG、LLM 等
业务类型。它只描述跨模块数据，不执行 DAG、Policy、Capability 或持久化操作。

## 测试

```bash
.venv/bin/python -m pytest harness-contracts/tests -v
```
