# FinanceClaw 第二阶段设计说明书

> **文档性质**：阶段实施设计 / Architecture Decision Baseline
> **阶段名称**：Stage 2 — Reliable Plan Execution Engine
> **版本**：V1.0
> **日期**：2026-08-25
> **前置基线**：第一阶段最小 Harness Runtime 已完成
> **依据文档**：`.design/Harness-Agent_通用可插拔智能体平台架构设计_修订版.md`、`.design/第一阶段.md`

---

## 0. 阶段结论

第二阶段的目标不是先做一个“更聪明的 Agent”，而是把第一阶段已经稳定的：

```text
Request
  ↓
HarnessRuntime
  ↓
Registry + Policy + Trace
  ↓
一个 Agent / Tool Capability
```

升级为：

```text
Request / Goal
     ↓
ExecutionPlan
     ↓
PlanValidator
     ↓
ExecutionEngine / Scheduler
     ↓
0..N Plan Nodes
     ↓
CapabilityInvoker
     ↓
Registry + Policy + Trace
     ↓
Agent / Tool
```

第二阶段的核心命题是：

> **给定一个合法的结构化 ExecutionPlan，Harness 能够可靠、可恢复、可取消、可审计地执行它。**

因此阶段二优先解决“计划如何可靠执行”，而不是“LLM 如何智能地产生计划”。

---

# 1. 第一阶段基线

当前本地维护基线已经完成：

```text
harness-contracts
harness-spi
harness-registry
harness-policy
harness-trace
harness-runtime
harness-plugin-local
harness-bootstrap
plugins/echo-agent
plugins/calculator-tool
plugins/mock-finance-agent
```

第一阶段已经证明：

1. 新增 Agent / Tool 不需要修改 Harness Core；
2. Plugin 只依赖 `harness-contracts + harness-spi`；
3. `HarnessRuntime.invoke()` 可以执行一个明确目标 Capability；
4. Registry / Policy / Trace 与业务 Plugin 解耦；
5. Bootstrap 负责应用级 Composition Root 和 Plugin 生命周期；
6. Plugin 实例可在 Application 生命周期内复用，而不是每个请求重新拉起；
7. 本地 Python Entry Point 可以自动发现插件；
8. Runtime 已支持基础 timeout、asyncio cancellation、统一错误与 ResultEnvelope；
9. Trace 已形成 `REQUEST → RUNTIME → POLICY / REGISTRY_RESOLVE / CAPABILITY → AGENT / TOOL` 层级。

当前已知的第一阶段约束包括：

- `Request.target.capability` 必填；
- Registry 同一 capability 当前只允许一个 Provider；
- Policy 只有 `PRE_EXECUTE`；
- ResultStatus 只有 `SUCCESS / FAILED / DENIED`；
- ExecutionState 只表示单次 Invocation 的简单状态；
- CancellationContext 是只读快照；
- Runtime 只直接调用一个 Capability；
- 尚无 Planner、DAG、StateStore、Approval、Async Resume。

第二阶段必须在兼容第一阶段 Direct Invocation 的前提下演进。

---

# 2. 第二阶段目标

## 2.1 必须完成

第二阶段必须实现以下能力：

```text
ExecutionPlan Contract
DAG Validation
Serial / Parallel / Join
Structured Conditions
CapabilityInvoker
Plan / Node Execution State
InMemoryStateStore
SQLiteStateStore
Checkpoint / Resume
Timeout / Deadline Propagation
Cancellation
Retry + Idempotency Guard
Partial Result
Async WAITING / Resume
Human Approval / Resume
PRE_PLAN + PRE_EXECUTE Policy
Plan / Node Trace
Minimal Execution Event Model
Planner SPI
StaticPlanner
Minimal RulePlanner
```

## 2.2 第二阶段完成后的核心能力

一个请求可以形成如下执行过程：

```text
Request
   ↓
ExecutionPlan
   ↓
PlanValidator
   ↓
ExecutionEngine
   │
   ├── n1 Tool ──────────────┐
   │                         │
   ├── n2 Tool ──────────────┤ parallel
   │                         │
   └─────────────────────────┘
              ↓
             Join
              ↓
          Approval
              ↓
             n4
              ↓
         ResultEnvelope
```

并且能够：

```text
进程 A 执行到 WAITING
        ↓
checkpoint 到 SQLite
        ↓
进程结束
        ↓
进程 B 启动
        ↓
load(plan_id)
        ↓
resume
        ↓
继续同一个 plan_id
```

---

# 3. 第二阶段非目标

第二阶段明确不做：

```text
× LLM Planner
× Intent Router / 统一智能 handle() 入口
× 多 Provider Selector
× Provider A/B / Canary / Health Routing
× Remote Plugin
× Worker / Pod Remote Execution
× MCP
× WorkflowSPI / CapabilityType.WORKFLOW
× Long-term MemoryProvider
× RAG / Vector DB
× 完整 ModelProvider 体系
× 完整 ConnectorProvider 体系
× 多 Agent 自治递归
× 动态 Plan Patch / Agent 自主修改主 DAG
× 分布式 Scheduler
× 分布式锁
× 多 Scheduler 竞争同一 Plan
× Kafka / Redis Stream / NATS 等外部 Event Bus
× 完整 Token / Cost Accounting
× SSE / Streaming Renderer
× Control Plane Catalog / Marketplace
```

第二阶段仍然是 **Data Plane Execution Engine** 的建设阶段。

---

# 4. 已定 Architecture Decisions

## ADR-P2-001：StateStore

阶段二正式引入：

```text
StateStore SPI
├── InMemoryStateStore
└── SQLiteStateStore
```

原则：

- `build_harness()` 默认仍使用 `InMemoryStateStore`；
- SQLite 用于真实验证 checkpoint / resume / crash recovery；
- SQLite 是单进程 / 单 writer 的参考持久化实现；
- 第二阶段不解决分布式写竞争；
- State 中预留 `state_version`，方便未来 CAS / optimistic concurrency 演进。

Deferred：

```text
分布式锁
多 Scheduler 并发写
Lease / Ownership
CAS / Optimistic Concurrency
Scheduler Failover
PostgreSQLStateStore
RedisStateStore
分布式任务抢占
```

---

## ADR-P2-002：Request target 与 Direct Invocation

阶段二将：

```python
Request.target: RequestTarget | None = None
```

但：

```text
HarnessRuntime.invoke(request)
```

仍然是严格的 Direct Invocation API。

如果：

```text
request.target is None
```

则返回：

```text
HARNESS.REQUEST.TARGET_REQUIRED
```

Plan Execution 走独立入口：

```text
HarnessApplication.execute_plan(request, plan)
```

未来 Router 阶段再增加：

```text
HarnessApplication.handle(request)
```

`RequestTarget.plugin` 第二阶段保留，作为 debug / test / administrative override；其长期 Provider pinning 语义在多 Provider 阶段重新决策。

---

## ADR-P2-003：Planner

阶段二引入独立 Planner SPI：

```text
Planner SPI
├── StaticPlanner
└── Minimal RulePlanner
```

Planner：

```text
只生成 ExecutionPlan
不执行 Capability
不直接访问业务 SDK
不持有 Provider 实现
不注册进 CapabilityRegistry
```

Planner 面向 `CapabilityCatalog` 的只读能力描述，而不是 Provider 对象。

第二阶段不实现 LLM Planner。

---

## ADR-P2-004：Human Approval

Human Approval 是 Execution Engine 的一等等待语义，不是 Capability。

支持两种来源：

```text
1. 显式 APPROVAL Plan Node
2. PolicyEffect.REQUIRE_APPROVAL
```

两种方式共享：

```text
ApprovalRequest
ApprovalDecision
WAITING
checkpoint
resume
```

Approval 不长时间占用 asyncio Task。

审批进入 WAITING 后，当前 API 调用返回；审批完成后通过 `resume` 继续同一个 `plan_id`。

Deferred：

```text
外部 Approval System Adapter
多级审批
会签
审批委托
审批超时升级
跨系统 Approval Event
```

---

## ADR-P2-005：ResultStatus

阶段二扩展：

```text
SUCCESS
PARTIAL
FAILED
DENIED
CANCELLED
ACCEPTED
```

语义固定如下：

| ResultStatus | 语义 |
|---|---|
| SUCCESS | 最终完整成功 |
| PARTIAL | 执行已结束，但仅得到部分有效结果 |
| FAILED | 最终执行失败 |
| DENIED | 最终被治理策略拒绝 |
| CANCELLED | 最终因取消而停止 |
| ACCEPTED | 当前 API 调用成功，但底层执行尚未结束 |

`ACCEPTED` 必须携带 continuation reference。

`ResultStatus`、`PlanExecutionStatus`、`NodeExecutionStatus` 分开建模。

---

## ADR-P2-006：Workflow

第二阶段：

```text
不实现 WorkflowSPI
不增加 CapabilityType.WORKFLOW
```

`ExecutionPlan + ExecutionEngine` 先作为 Workflow 的基础执行模型。

通过固定静态 Plan 验证 Workflow-like 场景。

Deferred：

```text
WorkflowSPI
WorkflowManifest
Workflow Versioning
Workflow-as-Capability
Nested Workflow
Workflow Catalog
Workflow recursion / depth limits
```

---

# 5. 第二阶段目标架构

```text
                         HarnessApplication
                                │
             ┌──────────────────┴──────────────────┐
             │                                     │
      Direct Invocation                       Plan Execution
             │                                     │
             ▼                                     ▼
      HarnessRuntime                         ExecutionEngine
             │                                     │
             │                              PlanValidator
             │                                     │
             │                                Scheduler
             │                                     │
             │                        ┌────────────┼────────────┐
             │                        ▼            ▼            ▼
             │                      Node A       Node B      Approval
             │                        │            │
             └──────────────┐         └──────┬─────┘
                            ▼                ▼
                     CapabilityInvoker ◄─────┘
                            │
               ┌────────────┼────────────┐
               ▼            ▼            ▼
           Registry       Policy       Trace
               │
               ▼
           Agent / Tool

ExecutionEngine
    │
    ├── StateStore
    ├── Cancellation
    ├── Deadline
    ├── Retry
    └── Execution Events
```

核心原则：

> Scheduler、Agent、Planner 都不能直接绕过 CapabilityInvoker 裸调 Provider。

---

# 6. 模块规划

## 6.1 现有模块扩展

### `harness-contracts`

新增或扩展：

```text
Request.target optional
ExecutionPlan
PlanNode
PlanEdge
PlanBudget
InputBinding / OutputBinding
ConditionExpr
RetryPolicy
FailurePolicy
PlanExecutionStatus
NodeExecutionStatus
PlanExecutionState
NodeExecutionState
ApprovalRequest
ApprovalDecision
Continuation
ResultIssue
ResultStatus extensions
Capability execution profile
```

保持业务无关。

---

### `harness-runtime`

阶段二重点重构：

```text
HarnessRuntime
      ↓
CapabilityInvoker
```

`CapabilityInvoker` 统一承担：

```text
Registry resolve
PRE_EXECUTE Policy
Capability span
Agent / Tool invocation
Timeout
Cancellation
Error normalization
Trace propagation
Result normalization
```

`HarnessRuntime.invoke()` 继续只负责 Direct Invocation 生命周期。

同时抽取 Direct 与 Plan 共用的 Invocation Context / Trace 生命周期辅助能力，避免 `execute_plan()` 复制第一阶段 Request Context 逻辑。

---

### `harness-registry`

第二阶段仍保持单 Provider 模型。

新增只读：

```text
CapabilityCatalog
```

Planner 只能看到 Capability 描述，例如：

```text
id
name
type
version
schemas
tags
execution profile
```

不能获取 Provider instance。

多 Provider / Selector 留到阶段三。

---

### `harness-policy`

扩展：

```text
PolicyPhase
├── PRE_PLAN
└── PRE_EXECUTE
```

`PolicyEffect`：

```text
ALLOW
DENY
REQUIRE_APPROVAL
```

Policy 只作决策，不自己等待 Approval。

---

### `harness-trace`

新增稳定执行语义：

```text
PLAN
PLAN_NODE
SCHEDULER
PLANNER
```

不为每个瞬时状态增加 SpanType。

例如以下应该是 Event / Attribute：

```text
node.ready
node.retrying
node.waiting
node.resumed
approval.requested
checkpoint.saved
```

---

### `harness-bootstrap`

新增组装：

```text
StateStore
CapabilityInvoker
PlanValidator
ExecutionEngine
optional Planner
ExecutionEventPublisher
```

默认：

```text
InMemoryStateStore
InMemoryEventBus / No-op publisher
```

不产生磁盘副作用。

---

## 6.2 新增模块

### `harness-planning`

职责：

```text
Planner SPI
PlanningContext
StaticPlanner
RulePlanner
PlanValidator
CapabilityCatalog View consumption
```

它不执行 Plan。

---

### `harness-execution`

职责：

```text
ExecutionEngine
Scheduler
DAG state machine
Condition evaluation
Input binding resolution
Retry
Deadline propagation
Cancellation coordination
Approval waiting / resume
Async waiting / resume
Final plan result composition
```

不包含业务逻辑。

---

### `harness-state`

职责：

```text
StateStore SPI
InMemoryStateStore
SQLiteStateStore
```

StateStore 只理解“保存/读取状态快照”，不实现 Scheduler 业务状态迁移。

---

### `harness-events`

阶段二只实现最小 in-process 执行事件模型：

```text
ExecutionEvent
EventPublisher
EventSubscriber
InMemoryEventBus
NoOpEventPublisher
```

不连接 Kafka / MQ。

该模块用于避免 ExecutionEngine 直接绑定未来 Metrics / Audit / Billing / UI 消费者。

---

# 7. ExecutionPlan Contract

## 7.1 ExecutionPlan

建议核心结构：

```yaml
plan_id: plan-001
revision: 1
budget:
  deadline_at: 2026-08-25T12:00:00Z
  max_concurrency: 4
  token_limit: null
  cost_limit: null

failure_policy: fail_fast

nodes: []
edges: []
outputs: {}
metadata: {}
```

字段含义：

```text
plan_id
= 一次逻辑计划的稳定 ID

revision
= 计划版本；阶段二不支持动态 Patch，但为后续演进保留

budget
= Plan 级执行预算

nodes / edges
= DAG

outputs
= 最终结果映射
```

---

## 7.2 PlanNode

```yaml
node_id: n1
kind: capability
capability: finance.mock-query/v1
input_mapping: {}
timeout_ms: 10000
retry_policy: standard
failure_policy: fail_plan
idempotency_key: null
policy_tags: [finance, read]
metadata: {}
```

`kind` 第二阶段只允许：

```text
CAPABILITY
APPROVAL
```

Approval Node 不注册进 Registry。

---

## 7.3 Input Mapping

第二阶段不使用：

```text
${n1.data.foo}
```

这类任意字符串表达式作为唯一协议。

建议使用结构化 Binding：

```text
LiteralBinding
RequestBinding
NodeOutputBinding
```

例如：

```yaml
amount:
  kind: node_output
  node_id: n1
  pointer: /output/data/amount
```

路径第一版使用 JSON Pointer 风格，避免引入完整 JSONPath / Expression DSL。

原则：

> 节点之间只能通过显式结构化映射传递数据，禁止依赖共享全局可变对象。

---

## 7.4 Edge 与 Condition

建议：

```yaml
from_node: n1
to_node: n2
trigger: success
condition:
  ref:
    node_id: n1
    pointer: /output/data/score
  operator: lt
  value: 0.8
```

`trigger` 支持：

```text
SUCCESS
FAILED
DENIED
COMPLETED
ALWAYS
```

Condition 第一版支持：

```text
eq
ne
lt
lte
gt
gte
exists
in
and
or
not
```

禁止：

```python
eval(user_expression)
```

也不允许根据自然语言输出 contains/regex 决定主 DAG。

---

## 7.5 Budget

`PlanBudget` 建议包含：

```text
deadline_at
max_concurrency
token_limit
cost_limit
```

阶段二真正强制：

```text
deadline_at
max_concurrency
```

`token_limit / cost_limit` 先冻结契约，等待 ModelProvider / Budget Engine 阶段真正 enforcement。

---

# 8. Execution State

## 8.1 PlanExecutionStatus

```text
CREATED
RUNNING
WAITING
SUCCEEDED
PARTIAL
FAILED
DENIED
CANCELLED
```

---

## 8.2 NodeExecutionStatus

```text
PENDING
READY
RUNNING
WAITING
SUCCEEDED
FAILED
DENIED
SKIPPED
CANCELLED
```

---

## 8.3 PlanExecutionState

建议包含：

```text
plan_id
plan_revision
state_version
status
nodes: Map<NodeId, NodeExecutionState>
issues
pending_approvals
pending_jobs
started_at
updated_at
completed_at
metadata
```

State 与 Context 保持分离。

---

## 8.4 NodeExecutionState

建议包含：

```text
node_id
status
attempt
started_at
completed_at
result
error
waiting_reason
continuation
```

Node 的最终输出通过 ResultEnvelope 保存或引用。

---

# 9. ResultEnvelope 第二阶段扩展

## 9.1 ResultStatus

```text
SUCCESS
PARTIAL
FAILED
DENIED
CANCELLED
ACCEPTED
```

---

## 9.2 Continuation

`ACCEPTED` 必须包含：

```text
Continuation
├── plan_id?
├── node_id?
├── job_ref?
├── approval_id?
└── waiting_reason
```

至少存在一个可继续追踪的引用。

---

## 9.3 ResultIssue

为 PARTIAL / 多节点问题增加：

```text
ResultIssue
├── source
├── error
└── metadata
```

不要用单个 `error` 表示整个 DAG 的所有局部失败。

---

## 9.4 状态验证规则

```text
SUCCESS
    output required
    error forbidden

PARTIAL
    output required
    issues non-empty

FAILED
    error required

DENIED
    error required

CANCELLED
    cancellation/error detail optional

ACCEPTED
    continuation required
    final output forbidden
```

第一阶段已有 SUCCESS / FAILED / DENIED API 保持兼容。

---

# 10. Capability Execution Profile

当前 CapabilityDescriptor 需要增加可靠调度所需通用语义。

建议增加：

```text
side_effect: none | read | write
egress: none | internal | external
idempotency: none | optional | required
```

注意：

```text
side_effect
```

和：

```text
egress
```

是正交维度。

例子：

```text
内部数据库查询
= read + internal

公开搜索 API
= read + external

发送外部邮件
= write + external
```

这些字段属于 Capability 层，而不是由业务 Runtime 猜测。

---

# 11. CapabilityInvoker

第二阶段需要从现有 Runtime 中抽取统一受控 Capability 调用边界：

```text
CapabilityInvoker.invoke(...)
```

其职责：

```text
resolve capability
policy PRE_EXECUTE
apply approved grant if present
build child trace context
invoke Agent / Tool
apply timeout
propagate cancellation
normalize HarnessError
validate ResultEnvelope
normalize trace id
```

调用者：

```text
HarnessRuntime
ExecutionEngine / Scheduler
未来 Agent Runtime Client
```

禁止：

```text
Scheduler → registry.resolve → provider.execute
Agent → registry.resolve → provider.execute
```

这会绕过 Policy / Trace / Deadline / Error normalization。

---

# 12. Planner SPI

建议：

```python
class Planner(ABC):
    async def plan(
        self,
        request: Request,
        context: PlanningContext,
        catalog: CapabilityCatalog,
    ) -> ExecutionPlan:
        ...
```

Planner 只产出结构化 Plan。

## 12.1 StaticPlanner

输入固定模板或 Plan Factory。

用途：

```text
Contract Test
Scheduler Test
Workflow-like Integration Test
```

---

## 12.2 Minimal RulePlanner

只做轻量、有序的规则选择：

```text
Request Predicate
      ↓
Plan Template / Plan Factory
```

不建设通用 Rule DSL、Rule Database、Hot Reload。

---

## 12.3 PlanningContext

Planner 只能获得规划所需最小上下文，例如：

```text
request
identity scopes
tenant
deadline
allowed capability catalog
budget
planning attributes
```

不允许 Planner 获得 Secret、Provider instance 或 Runtime mutable state。

---

# 13. PlanValidator

任何 Plan 在进入 Scheduler 前必须验证。

至少验证：

```text
plan_id / revision 合法
node_id 唯一
edge source / target 存在
DAG 无环
至少一个 root
不存在悬空 output reference
input mapping reference 有效
condition reference 有效
node timeout > 0
retry 参数合法
plan deadline 合法
node capability 字段与 node kind 一致
approval node 不携带 capability
capability node 必须携带 capability
```

在执行前还可以使用 CapabilityCatalog 做 executable validation：

```text
Capability 是否存在
Capability type / basic contract 是否合理
```

第二阶段不尝试做完整 JSON Schema 静态类型推导。

---

# 14. Scheduler 执行语义

## 14.1 基础调度

执行算法原则：

```text
validate plan
    ↓
create execution state
    ↓
roots → READY
    ↓
max_concurrency semaphore
    ↓
run ready nodes
    ↓
checkpoint terminal node
    ↓
evaluate outgoing edges
    ↓
unlock next nodes
    ↓
until terminal / waiting
```

---

## 14.2 串行

```text
A → B → C
```

B 只有在 A 对应 incoming dependency 满足后才能 READY。

---

## 14.3 并行

无依赖节点并行执行：

```text
       ┌→ B
A ─────┤
       └→ C
```

受：

```text
plan.max_concurrency
```

限制。

---

## 14.4 Join

Join Node 只有在所有需要参与的 activated predecessor 都到达 terminal 状态后才可判断是否 READY。

不满足任何激活边的分支节点最终标记为：

```text
SKIPPED
```

---

## 14.5 Failure Policy

节点建议支持：

```text
FAIL_PLAN
CONTINUE
```

默认：

```text
FAIL_PLAN
```

如果 `CONTINUE` 节点失败，而 Plan 仍能生成有效最终输出：

```text
Plan → PARTIAL
```

如果最终 output mapping 的必需来源无法产生：

```text
Plan → FAILED
```

条件分支正常造成的 `SKIPPED` 不自动意味着 PARTIAL。

---

# 15. Retry 与 Idempotency

建议 `RetryPolicy`：

```text
max_attempts
initial_backoff_ms
max_backoff_ms
multiplier
```

阶段二参考实现不要求 jitter，保证测试确定性。

自动 Retry 必须同时满足：

```text
ErrorDetail.retryable == true
+
Idempotency Rule 允许
+
还有剩余 deadline
```

规则：

```text
side_effect = none/read
→ 可按 retryable 自动 retry

side_effect = write, idempotency = required
→ 必须有 idempotency_key 才允许 retry

side_effect = write, idempotency = optional
→ 只有明确提供 idempotency_key 时自动 retry

side_effect = write, idempotency = none
→ 禁止自动 retry
```

每次 Retry 不能重新获得完整 timeout budget。

---

# 16. Deadline

Deadline 统一采用绝对时间。

有效 Node Deadline：

```text
effective_deadline
= min(
    InvocationContext.deadline_at,
    ExecutionPlan.budget.deadline_at,
    now + node.timeout_ms
  )
```

原则：

```text
子 Node deadline 不得超过父 Plan deadline
Retry 共享原 Node deadline
Resume 不重置原始计划 deadline
```

如果 Resume 时 deadline 已过：

```text
Plan → FAILED / timeout
```

---

# 17. Cancellation

第二阶段区分：

```text
CancellationContext
= 可序列化只读快照

Runtime Cancellation Signal
= 进程内实时取消机制
```

Python 阶段二使用：

```text
asyncio Task cancellation
+
Scheduler internal cancellation state
```

取消流程：

```text
cancel(plan_id)
    ↓
checkpoint cancellation requested
    ↓
停止调度新 Node
    ↓
取消当前运行 Task
    ↓
remaining pending nodes → CANCELLED
    ↓
Plan → CANCELLED
```

取消不归类为 FAILED。

---

# 18. Async Node

Provider 可以返回：

```text
ResultStatus.ACCEPTED
+
Continuation.job_ref
```

Scheduler：

```text
Node → WAITING
Plan → WAITING（如果没有其他可执行节点）
checkpoint
当前 API 返回 ACCEPTED
```

阶段二提供一个明确的 completion ingress：

```text
complete_async_node(plan_id, node_id, terminal_result)
```

terminal result 只能是：

```text
SUCCESS
PARTIAL
FAILED
DENIED
CANCELLED
```

之后 Engine checkpoint 并 resume 调度。

阶段二不实现通用 callback server / polling framework / event broker adapter。

---

# 19. Human Approval

## 19.1 Explicit Approval Node

```text
CAPABILITY n1
    ↓
APPROVAL n2
    ↓
CAPABILITY n3
```

Scheduler 到达 n2：

```text
create ApprovalRequest
n2 → WAITING
Plan → WAITING
checkpoint
return ACCEPTED
```

审批完成：

```text
resolve_approval(...)
    ↓
APPROVED → n2 SUCCEEDED
REJECTED → n2 DENIED
    ↓
resume
```

Edge 可以根据 SUCCESS / DENIED 分流。

---

## 19.2 Policy-triggered Approval

Capability Node PRE_EXECUTE：

```text
Policy → REQUIRE_APPROVAL
```

Scheduler：

```text
该 Capability Node → WAITING
生成 ApprovalRequest
checkpoint
return ACCEPTED
```

批准后必须携带结构化 Approval Grant 再次进入 PRE_EXECUTE，使 Policy 能区分“已获批准”，避免审批循环。

拒绝：

```text
Node → DENIED
```

---

## 19.3 Approval Security

ApprovalRequest 只保存安全摘要：

```text
capability
resource category
side effect
egress
parameter summary
reason
```

禁止写入：

```text
Secret 明文
完整数据库密码
敏感 Token
不必要的完整 Prompt
```

---

# 20. StateStore

建议 SPI 保持小：

```python
class StateStore(ABC):
    async def create(self, record: PlanExecutionRecord) -> None: ...
    async def load(self, plan_id: str) -> PlanExecutionRecord | None: ...
    async def save(self, record: PlanExecutionRecord) -> None: ...
    async def delete(self, plan_id: str) -> None: ...
```

不要让 StateStore 出现：

```text
mark_node_ready()
retry_node()
approve_node()
```

这些属于 ExecutionEngine 状态机。

---

## 20.1 Checkpoint 边界

第二阶段必须 checkpoint：

```text
Plan 创建后
Node 调用前
Node terminal result 后
进入 WAITING 前
收到 Approval / Async completion 后
Plan terminal 前
Cancellation 状态变化后
```

不要求把 Scheduler 每一个瞬时内存状态都持久化。

---

## 20.2 SQLiteStateStore

阶段二参考存储结构建议简单使用 JSON Snapshot：

```text
plan_id PRIMARY KEY
state_version
payload_json
created_at
updated_at
```

不在阶段二提前把所有 Node State 关系型拆表。

SQLite 目标是验证：

```text
Serialization
Atomic snapshot save
Crash recovery
Resume
```

而不是生产分布式吞吐。

---

## 20.3 Resumable Context Snapshot

为了跨进程 Resume，StateStore 必须保存最小可恢复上下文快照，包括：

```text
Request
trusted identity subject/scopes
trusted tenant identity
deadline
trace identifiers
必要的非敏感 invocation attributes
```

禁止持久化：

```text
Secret 明文
live Python object
DB connection
HTTP client
asyncio Task
Cancellation token object
```

未来 Secret / Credential 必须通过引用重新解析。

---

# 21. Policy 第二阶段

Policy 扩展成：

```text
PRE_PLAN
PRE_EXECUTE
```

PRE_PLAN 可以判断：

```text
是否允许执行该计划
最大节点数
允许的 capability scope
允许的 side effect / egress
预算约束
```

PRE_EXECUTE 每个节点继续检查：

```text
identity / tenant
capability permission
side effect
egress
approval
```

PolicyEngine 不负责：

```text
等待审批
执行 Plan
执行 Provider
修改 DAG
```

---

# 22. Trace

第二阶段推荐 Trace：

```text
REQUEST
└── RUNTIME
    └── PLAN
        ├── SCHEDULER
        ├── PLAN_NODE n1
        │   └── CAPABILITY
        │       └── TOOL / AGENT
        ├── PLAN_NODE n2
        │   └── CAPABILITY
        │       └── TOOL / AGENT
        └── PLAN_NODE approval
```

若 Planner 在同一链路中执行：

```text
REQUEST
└── RUNTIME
    ├── PLANNER
    └── PLAN
```

业务名称放：

```text
Span.name
Span.attributes
```

不要新增大量业务 SpanType。

---

# 23. Execution Events

阶段二最小事件：

```text
plan.created
plan.started
plan.waiting
plan.resumed
plan.completed
plan.failed
plan.cancelled

node.ready
node.started
node.retrying
node.waiting
node.completed
node.failed
node.denied
node.cancelled

approval.requested
approval.resolved

async.accepted
async.completed

checkpoint.saved
```

事件消费者第一版：

```text
InMemory subscriber
Test observer
```

Trace 仍由 Tracer 负责，Event 不代替 Span。

重要状态转换建议：

```text
先 checkpoint
再发布对应 terminal/waiting event
```

避免观察者看到一个尚未可恢复的状态。

阶段二不保证 durable event delivery / exactly-once。

Deferred：

```text
Transactional Outbox
Durable Event Bus
Kafka / NATS / Redis Stream
Exactly-once Delivery
Billing / Audit Consumers
```

---

# 24. HarnessApplication API

第二阶段建议形成显式 API：

```python
await app.invoke(request)
```

Direct Capability Invocation，要求 target。

```python
await app.execute_plan(request, plan)
```

创建并推进 Plan。

```python
await app.resume_plan(plan_id)
```

从 StateStore 恢复并继续。

```python
await app.cancel_plan(plan_id, reason=...)
```

取消 Plan。

```python
await app.resolve_approval(plan_id, approval_id, decision)
```

处理审批并继续。

```python
await app.complete_async_node(plan_id, node_id, result)
```

提交异步 Provider 最终结果并继续。

第二阶段不增加：

```python
app.handle(request)
```

统一智能入口留给 Router 阶段。

---

# 25. Milestones

## P2-M0：Contract Freeze

实现：

```text
ExecutionPlan
PlanNode / Edge
Binding / Condition
Budget
RetryPolicy
FailurePolicy
Plan / Node State
Approval contracts
Continuation
ResultIssue
ResultStatus extension
Capability execution profile
Request.target optional
```

验收：

```text
JSON round-trip
Frozen / mutable boundary test
invalid contract rejection
第一阶段 contract test 兼容
```

---

## P2-M1：CapabilityInvoker Refactor

实现：

```text
CapabilityInvoker
Direct Runtime 改为复用 Invoker
共享 Context / Trace lifecycle helper
```

验收：

```text
第一阶段全部测试继续通过
Direct invoke 行为不变
Policy / Trace / timeout / error semantics 不退化
```

---

## P2-M2：Planning + Basic DAG

实现：

```text
CapabilityCatalog
Planner SPI
StaticPlanner
RulePlanner
PlanValidator
ExecutionEngine
serial
parallel
join
conditions
input/output mapping
```

验收：

```text
确定性静态 DAG 可重复执行
无环检查
并发限制生效
结构化条件正确
```

---

## P2-M3：Reliability

实现：

```text
Retry
Idempotency guard
Deadline propagation
Cancellation
Failure policy
Partial result
```

验收：

```text
retryable transient error
write retry protection
timeout across retries
client cancellation
parallel task cancellation
best-effort partial result
```

---

## P2-M4：Persistence / Resume

实现：

```text
StateStore SPI
InMemoryStateStore
SQLiteStateStore
checkpoint
resume
resumable context snapshot
```

验收：

```text
执行中断
关闭进程级 Application
重新 build/start
load same plan_id
继续完成
```

---

## P2-M5：Waiting / Approval / Async / Governance

实现：

```text
Approval Node
Policy REQUIRE_APPROVAL
Async ACCEPTED
WAITING
resume ingress
PRE_PLAN policy
Plan / Node trace
Execution Events
```

验收：

```text
Approval pause → process restart → approve → resume
Approval reject
Policy-driven approval
Async accepted → process restart → completion → resume
Trace hierarchy complete
Event ordering correct
```

---

# 26. 测试策略

## 26.1 Contract Test

覆盖：

```text
Plan schema
DAG contract
Result status validation
Approval model
Retry / capability profile
State serialization
```

---

## 26.2 Plan Validator Test

覆盖：

```text
cycle
missing node
invalid reference
invalid approval node
invalid timeout
invalid binding
invalid condition
```

---

## 26.3 Scheduler Test

覆盖：

```text
serial
parallel
join
branch
skip
fail-fast
continue-on-failure
partial
max concurrency
```

---

## 26.4 Reliability / Fault Injection

覆盖：

```text
transient failure
retry exhausted
unsafe write retry rejected
timeout
cancel before run
cancel while running
provider exception
invalid provider result
```

---

## 26.5 State / Resume Test

覆盖：

```text
InMemory round-trip
SQLite round-trip
checkpoint
restart
resume
waiting resume
corrupt / missing state
```

---

## 26.6 Approval Test

覆盖：

```text
explicit approval
approve
reject
approval persistence
policy approval
approval grant prevents loop
```

---

## 26.7 Async Test

覆盖：

```text
ACCEPTED validation
missing continuation rejected
WAITING persistence
complete async success
complete async failure
wrong job/node completion rejected
```

---

## 26.8 Trace / Event Test

覆盖：

```text
REQUEST → RUNTIME → PLAN → PLAN_NODE → CAPABILITY
parallel node parent relationship
retry event
waiting event
resume event
cancelled spans
```

---

## 26.9 Compatibility Test

阶段一所有测试必须作为阶段二 regression suite 的固定部分。

原则：

> Stage 2 不能以破坏 Stage 1 Direct Invocation 为代价。

---

# 27. 第二阶段端到端验收场景

建议新增一个确定性的 `finance-review-plan` 示例，而不是立即引入 LLM。

例如：

```text
             ┌── n1 finance.mock-query/v1
Request ─────┤
             └── n2 math.calculate/v1
                      │
                      ▼
                     Join
                      │
                      ▼
                 n3 Approval
                      │
                      ▼
                 n4 echo.reply/v1
```

在不同测试中模拟：

```text
n1 transient failure → retry success
n2 permanent failure + CONTINUE → PARTIAL
approval → WAITING
restart process
approval accepted
resume
n4 success
final result
```

最终一次完整 Trace 至少可以看到：

```text
REQUEST
└── RUNTIME
    └── PLAN finance-review-plan
        ├── PLAN_NODE n1
        │   └── CAPABILITY
        │       └── AGENT
        ├── PLAN_NODE n2
        │   └── CAPABILITY
        │       └── TOOL
        ├── PLAN_NODE n3 approval
        └── PLAN_NODE n4
            └── CAPABILITY
                └── AGENT
```

---

# 28. Definition of Done

只有满足以下条件，第二阶段才算完成：

1. 第一阶段全部 regression tests 继续通过；
2. Direct `HarnessRuntime.invoke()` API 保持兼容；
3. 一个 Plan 能执行多个 Capability；
4. DAG 支持串行、并行、Join、条件分支；
5. Scheduler 不直接调用 Provider；
6. 所有 Capability Node 都经过 CapabilityInvoker；
7. Timeout 与 Deadline 在整个 Plan 中正确传播；
8. Cancellation 可以停止新调度并取消运行节点；
9. Retry 不会绕过 Idempotency 规则；
10. SQLite 可以真正完成进程重启后的 Resume；
11. Approval 不依赖挂起长期 asyncio Task；
12. Async ACCEPTED 节点可以持久化 WAITING 并恢复；
13. PARTIAL / FAILED / DENIED / CANCELLED / ACCEPTED 语义清晰可测试；
14. Policy 支持 PRE_PLAN、PRE_EXECUTE、REQUIRE_APPROVAL；
15. Trace 可以看到完整 Plan / Node 层级；
16. Plugin 仍只依赖 Contracts / SPI；
17. Planner 不执行 Capability；
18. Registry 第二阶段仍不退化成 Service Locator；
19. 所有新增核心契约都有单元测试和 JSON round-trip 测试；
20. Deferred 问题被明确记录，不通过隐式实现提前锁死后续架构。

---

# 29. Deferred / 下一阶段演进记录

以下问题阶段二**故意不解决**，但必须持续保留：

## 29.1 State / Distributed Execution

```text
分布式锁
多 Scheduler 并发写
Lease / Ownership
CAS
Scheduler Failover
Distributed Queue
Task Stealing
PostgreSQL / Redis StateStore
```

## 29.2 Provider / Registry

```text
同 Capability 多 Provider
Provider Selector
Health
Cost / Latency / Quality Routing
Tenant visibility
A/B
Canary
Provider pinning 最终语义
```

## 29.3 Workflow

```text
WorkflowSPI
Workflow-as-Capability
Nested Workflow
Workflow Catalog
Workflow Versioning
```

## 29.4 Planner / Agentic

```text
Intent Router
LLM Planner
Hybrid Planner
PlanPatchProposal
Agent dynamic plan extension
Agent recursion limits
```

## 29.5 Async / Event

```text
Callback Adapter
Polling Framework
Durable Event Bus
Transactional Outbox
Exactly-once event delivery
```

## 29.6 Platform / Control Plane

```text
Remote Plugin
Worker
Catalog
Tenant Configuration
Quota
SecretProvider
Approval Service Integration
Plugin Governance
```

---

# 30. 推荐实现顺序

严格按以下顺序推进：

```text
1. Contracts
      ↓
2. CapabilityInvoker
      ↓
3. PlanValidator
      ↓
4. Basic Scheduler
      ↓
5. Retry / Deadline / Cancellation
      ↓
6. StateStore / SQLite
      ↓
7. Resume
      ↓
8. Approval
      ↓
9. Async WAITING
      ↓
10. Policy / Trace / Events 完整化
      ↓
11. End-to-End / Fault Injection / Restart Test
```

不要先做 UI、LLM Planner 或 Remote Provider。

---

# 31. 架构红线

第二阶段 Code Review 必须检查：

```text
harness-execution
```

禁止出现：

```text
finance.*
sql.*
rag.*
具体模型 SDK
具体业务 Provider class
```

Planner 禁止：

```text
直接 execute Tool
直接 invoke Agent
直接访问 DB / HTTP Business API
```

Scheduler 禁止：

```text
registry.resolve(...) 后裸调 provider
```

Plugin 仍禁止 import：

```text
harness-runtime.internal
harness-execution.internal
harness-registry.impl
harness-policy.impl
harness-state.impl
```

业务 Plugin 原则上继续只依赖：

```text
harness-contracts
harness-spi
```

---

# 32. 第二阶段完成后的演进位置

阶段二完成后，系统将从：

```text
可插拔 Capability Runtime
```

升级为：

```text
可插拔 + 可编排 + 可恢复的 Execution Platform
```

此时第三阶段才适合在稳定底座上引入：

```text
Multi Provider
Provider Selector
ModelProvider
ConnectorProvider
MemoryProvider
Fallback
Replay Eval
```

然后再进一步进入 Remote / Worker / Control Plane 平台化。

---

# 33. 一句话原则

> **Stage 1 证明“任何本地 Capability 都能被 Harness 安全调用”；Stage 2 证明“任何结构化 Capability DAG 都能被 Harness 可靠执行并恢复”。**
