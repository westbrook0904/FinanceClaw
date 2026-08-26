# FinanceClaw 第三阶段设计说明书

> **文档性质**：阶段实施设计 / Architecture Decision Baseline  
> **阶段名称**：Stage 3 — Adaptive Multi-Provider & Agentic Orchestration  
> **版本**：V0.9（讨论稿）  
> **日期**：2026-08-26  
> **前置基线**：Stage 1 Minimal Harness + Stage 2 Reliable Plan Execution Engine  
> **依据文档**：`.design/Harness-Agent_通用可插拔智能体平台架构设计_修订版.md`、`.design/第一阶段.md`、`.design/FinanceClaw-第二阶段说明书.md`

---

# 0. 阶段结论

第三阶段的目标不是“加一个会自由调用所有工具的 MainAgent”。

第三阶段要完成两个连续升级：

```text
Stage 2:
可插拔 + 可编排 + 可恢复的 Execution Platform

             ↓

Stage 3A:
同一 Capability 的多个 Provider 可选择、可降级、可评测

             ↓

Stage 3B / 3C:
模型可以可靠地 Route / Plan / Explore，
但实际执行仍由 Harness 控制
```

一句话：

> **让 FinanceClaw 从“可靠执行给定 Plan”升级为“能够可靠选择 Provider，并安全使用模型进行路由、规划和受限探索”。**

---

# 1. Stage 2 基线

Stage 2 已经提供：

```text
ExecutionPlan
PlanValidator
Scheduler
CapabilityInvoker
Retry / Deadline / Cancellation
InMemory / SQLite StateStore
Checkpoint / Resume
Approval / Policy Approval
Async WAITING / Completion
PRE_PLAN / PRE_EXECUTE Policy
Plan / Node Trace
Execution Events
Fault Injection / Restart Tests
```

因此 Stage 3 不重新实现：

```text
DAG
Retry
Approval
Async Resume
Checkpoint
FailurePolicy
```

而是把新的决策能力接到这些可靠执行边界上。

---

# 2. Stage 3 必须完成的能力

第三阶段拆为四个内部 Milestone。

## Stage 3A — Provider Fabric

```text
Provider Identity / Descriptor
Registry 1:N
ProviderSelector
SelectionContext / SelectionDecision
Health / Eligibility
Retry vs Fallback
Provider Pinning
A/B / Canary
ModelProvider
Provider Selection Trace / Events
```

## Stage 3B — Routing & Planning

```text
ExecutionMode
HarnessApplication.handle()
Router SPI
RuleRouter
LLMRouter
RouteDecision
LLMPlanner
HybridPlanner
Structured Plan Generation
Bounded Plan Repair
PRE_ROUTE Policy
Route / Plan Eval
```

## Stage 3C — Agentic Exploration

```text
ExplorationEngine
ExplorationBudget
ActionProposal
ScopedActionExecutor
Bounded ReAct
Explore Checkpoint
EXPLORE mode
HYBRID mode
PlanPatchProposal
Plan Revision Validation
Agentic Scope / Recursion Limits
```

## Stage 3D — Provider Expansion & Replay Eval

```text
ConnectorProvider
MemoryProvider
Selection Replay
Route Replay
Plan Replay / Validation Eval
Provider Comparison
E2E / Fault Injection
```

---

# 3. Stage 3 非目标

明确不做：

```text
× Remote Plugin
× Worker / Pod Remote Execution
× 分布式 Scheduler
× Distributed Lock / Lease
× Kafka / NATS / Redis Stream
× Control Plane Catalog
× Plugin Marketplace
× 完整租户配置中心
× 完整 SecretProvider 平台
× WorkflowSPI / Workflow Catalog
× 无限 Agent recursion
× Agent 自主直接修改主 DAG
× Agent 直接访问 Provider instance
× Agent 直接访问数据库 / HTTP Business API
× 完整 Long-term RAG Platform
× 任意厂商模型 SDK 泄漏到 Harness Core
```

这些继续留到 Stage 4 或之后。

---

# 4. Architecture Decisions

## ADR-P3-001：Capability 与 Provider Identity 分离

必须区分：

```text
capability_id
provider_id
plugin_id
```

例如：

```text
capability_id = data.query/v1
provider_id   = finance-query-primary
plugin_id     = finance-query-plugin
```

Planner / Explorer 面向 `capability_id`。

Selector 才选择 `provider_id`。

---

## ADR-P3-002：Registry 从 1:1 升级为 1:N

Stage 2：

```text
Capability → Provider
```

Stage 3：

```text
Capability
├── Provider A
├── Provider B
└── Provider C
```

Registry 负责候选集，不负责复杂选择策略。

---

## ADR-P3-003：ProviderSelector 独立于 Registry

```text
Registry:
    “有哪些候选？”

Selector:
    “这一次选谁？”

CapabilityInvoker:
    “如何安全调用？”
```

新增建议模块：

```text
harness-selection
```

---

## ADR-P3-004：Retry 与 Fallback 分离

Retry：

```text
A → A → A
```

Fallback：

```text
A → B → C
```

记录不同 Attempt 层级：

```text
NodeExecution
  ├── ProviderAttempt A
  │    ├── retry 1
  │    └── retry 2
  └── ProviderAttempt B
       └── retry 1
```

---

## ADR-P3-005：WRITE Fallback 必须 Fail Closed

默认：

```text
NONE / READ
    → 可自动 fallback

WRITE + stable idempotency + equivalent provider group
    → 可受控 fallback

WRITE + non-idempotent
    → 禁止自动 fallback
```

Provider 切换不能绕过 Stage 2 Idempotency Guard。

---

## ADR-P3-006：Execution Mode 为一等 Contract

```text
AUTO
FAST
PLAN
EXPLORE
HYBRID
```

高级调用方可以固定模式；普通用户默认 AUTO。

---

## ADR-P3-007：统一 `handle()`，保留低层 API

新增：

```python
await app.handle(request)
```

保留：

```python
invoke()
execute_plan()
resume_plan()
resolve_approval()
complete_async_node()
```

`handle()` 是 orchestration facade，不替代可靠执行 API。

---

## ADR-P3-008：Router 只做 Route

Router 不执行 Capability。

输出：

```text
RouteDecision
```

---

## ADR-P3-009：LLM Planner 只产出 ExecutionPlan

LLMPlanner：

```text
Goal + Catalog
      ↓
ModelProvider
      ↓
Structured ExecutionPlan
      ↓
PlanValidator
```

禁止：

```text
Planner → Tool
Planner → DB
Planner → Provider instance
```

---

## ADR-P3-010：Explore 使用 Harness-owned ExplorationEngine

Stage 3 默认不让普通 AgentPlugin 获得 CapabilityInvoker。

采用：

```text
ExplorationEngine
      ↓
Model decision
      ↓
ActionProposal
      ↓
ScopedActionExecutor
      ↓
CapabilityInvoker
```

这样保留：

```text
Policy
Trace
Deadline
Approval
Provider Selection
Idempotency
```

---

## ADR-P3-011：模型没有执行权

模型允许输出：

```text
RouteDecision
ExecutionPlan
ActionProposal
PlanPatchProposal
Final structured result
```

模型禁止：

```text
直接执行 Provider
直接修改 StateStore
直接修改主 DAG
直接授予自己权限
```

---

## ADR-P3-012：PlanPatch 是受治理的 Plan Revision

```text
Proposal
  ↓
validate
  ↓
policy
  ↓
revision++
  ↓
checkpoint
  ↓
continue
```

已完成节点历史不可被 Patch 重写。

---

## ADR-P3-013：不持久化隐藏 Chain-of-Thought

存储：

```text
decision_summary
reason_code
selected_action
observation_summary
evidence_refs
```

不要求保存：

```text
模型自由文本隐藏思维过程
```

---

## ADR-P3-014：Memory != StateStore

```text
StateStore
    → execution truth

MemoryProvider
    → conversation / domain memory
```

Memory 不参与恢复执行的真相判断。

---

## ADR-P3-015：Replay 默认不重新执行 WRITE

第一版 Replay Eval：

```text
Selection Replay
Route Replay
Plan Validation Replay
```

不会重新执行真实副作用节点。

---

# 5. 目标架构

```text
                         HarnessApplication.handle()
                                   │
                                   ▼
                             PRE_ROUTE Policy
                                   │
                                   ▼
                              Intent Router
                                   │
              ┌────────────────────┼─────────────────────┐
              ▼                    ▼                     ▼
            FAST                  PLAN             EXPLORE / HYBRID
              │                    │                     │
              │                 Planner            ExplorationEngine
              │                    │                     │
              │                    ▼                     │
              │             ExecutionPlan                │
              │                    │                     │
              │               PlanValidator              │
              │                    │                     │
              │                    ▼                     │
              │             ExecutionEngine              │
              │                    │                     │
              └──────────────┬─────┴──────────────┬──────┘
                             ▼                    ▼
                      CapabilityInvoker     ScopedActionExecutor
                             ▲                    │
                             └────────────────────┘
                             │
                      Registry candidates
                             │
                             ▼
                       ProviderSelector
                             │
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
          Agent             Tool            Model
            │                │                │
            └──────────── Connector / Memory ┘
```

---

# 6. 新增 / 修改模块

## 6.1 `harness-contracts`

新增：

```text
ExecutionMode
RouteDecision
ProviderDescriptor
ProviderHealthSnapshot
SelectionContext
SelectionDecision
ProviderAttempt
ExplorationBudget
ActionProposal
Observation
ExplorationState
ExplorationResult
PlanPatchProposal
PlanRevisionResult
```

---

## 6.2 `harness-registry`

修改为：

```text
register provider
unregister provider
list providers
candidates(capability)
get provider by provider_id
```

不能把 Selector 逻辑塞回 Registry。

---

## 6.3 `harness-selection`（新增）

```text
ProviderSelector SPI
PrioritySelector
ConstraintSelector
WeightedCanarySelector
FallbackPlanner
HealthSource
```

---

## 6.4 `harness-model`（新增）

第一版：

```text
ModelProvider SPI
ModelGateway
GenerateRequest / GenerateResult
EmbedRequest / EmbedResult（可以延后到 3D）
MockFastModel
MockQualityModel
```

Router / Planner / Explorer 只依赖 ModelGateway / Model SPI。

---

## 6.5 `harness-routing`（新增）

```text
Router SPI
RuleRouter
LLMRouter
RouteDecision validation
```

---

## 6.6 `harness-planning`

扩展：

```text
LLMPlanner
HybridPlanner
PlanningAttempt
Plan repair loop
PlanPatch validation
```

---

## 6.7 `harness-agentic`（建议新增）

```text
ExplorationEngine
ScopedActionExecutor
ExplorationBudgetGuard
ExplorationCheckpoint
ActionValidator
PlanPatchCoordinator
```

第一版不新增万能 `AgenticAgentSPI`。

---

## 6.8 `harness-connectors`（3D）

```text
ConnectorProvider SPI
QueryConnector
RetrieverConnector
MockQueryConnector
MockRetrieverConnector
```

---

## 6.9 `harness-memory`（3D）

```text
MemoryProvider SPI
load
search
write
compact

InMemoryMemoryProvider
```

不在本阶段接真实 Vector DB。

---

## 6.10 `harness-eval`（3D）

```text
SelectionReplay
RouteReplay
PlanReplay
EvalCase
EvalResult
```

---

# 7. Provider Contracts

## 7.1 ProviderDescriptor

建议：

```python
ProviderDescriptor(
    provider_id,
    capability_id,
    plugin_id,
    implementation_version,
    execution_profile,
    priority,
    tags,
    region,
    tenant_visibility,
    metadata,
)
```

运行时动态信息不要全部塞入 Descriptor：

```text
health
observed latency
observed quality
recent error rate
```

这些属于 Snapshot / Metrics。

## 7.2 Provider Equivalence Group

为 WRITE fallback 预留：

```text
equivalence_group
```

只有明确声明“两个 Provider 对同一 idempotency key 具有等价副作用语义”时，才能允许跨 Provider 自动 WRITE fallback。

---

# 8. Selection

## 8.1 SelectionContext

至少：

```text
request_id
tenant
capability_id
side_effect
egress
deadline
budget
provider_pin
canary_subject
policy_constraints
```

## 8.2 SelectionDecision

```text
selected_provider
eligible_candidates
rejected_candidates + reason_code
selector
reason_code
selection_key
```

不要在 Trace 中泄漏 Secret / 输入全文。

## 8.3 Eligibility → Ranking → Selection

推荐三阶段：

```text
Policy / compatibility filtering
          ↓
Eligibility
          ↓
Ranking
          ↓
Selection
```

---

# 9. Health

Stage 3 只做数据面可用的最小 Health。

支持：

```text
HEALTHY
DEGRADED
UNHEALTHY
UNKNOWN
```

第一版来源：

```text
StaticHealthSource
PassiveInvocationHealth
TestHealthSource
```

远程主动 health endpoint / Kubernetes readiness 留给 Stage 4。

---

# 10. Canary / A-B

使用稳定 hash bucket，不使用纯随机。

例如：

```text
hash(tenant_id + stable_subject) % 100
```

```text
0..89  → Provider A
90..99 → Provider B
```

目标：

```text
可重复
可回放
同主体稳定
```

---

# 11. Provider Pinning

Stage 2 的 `RequestTarget.plugin` 不再承担最终 pin 语义。

建议独立：

```text
ProviderPin
```

只允许：

```text
debug
test
admin
replay
```

普通业务调用不能通过 pin 绕过 Policy / Canary / Tenant Visibility。

---

# 12. ModelProvider

## 12.1 为什么先做 ModelProvider

如果先写：

```python
LLMRouter(openai_client)
LLMPlanner(openai_client)
```

Harness Core 会重新绑定厂商 SDK。

正确顺序：

```text
ModelProvider
     ↓
ModelGateway
     ↓
LLMRouter / LLMPlanner / ExplorationEngine
```

## 12.2 第一版范围

必须：

```text
generate
structured output
usage metadata
latency
provider identity
timeout / cancellation
```

可以延后：

```text
vision
embed
rerank
streaming
```

---

# 13. Execution Mode Contract

推荐把模式放入请求级稳定协议。

候选方案：

```python
RequestOptions(
    execution_mode=ExecutionMode.AUTO
)
```

`handle(request, mode=...)` 可以作为 API sugar，但最终应归一化到 Request / Invocation Context。

模式：

```text
AUTO
FAST
PLAN
EXPLORE
HYBRID
```

---

# 14. Router

## 14.1 RouteDecision

必须是结构化结果。

```text
mode
route_type
capability_id?
planner_id?
explorer_id?
confidence?
reason_code
```

## 14.2 RuleRouter

第一步先做 deterministic Router，建立 contract test。

## 14.3 LLMRouter

LLMRouter 只能：

```text
读取安全 Request summary
读取 CapabilityCatalog summary
调用 ModelGateway
返回 RouteDecision
```

不得执行任何业务 Capability。

---

# 15. LLM Planner

## 15.1 输入

```text
Goal
PlanningContext
CapabilityCatalog Snapshot
Policy-visible constraints
Budget
```

## 15.2 输出

```text
ExecutionPlan
```

## 15.3 可靠生成

必须：

```text
structured schema
parse
PlanValidator
bounded repair
```

建议：

```text
max_plan_attempts = 3
```

超过后返回明确 Planning Error，不进入 ExecutionEngine。

## 15.4 Plan Repair

只允许根据 Validator 的结构化错误修复 Plan。

不能：

```text
Validator failed
→ Planner 直接执行 Tool 试试看
```

---

# 16. `handle()`

建议：

```python
async def handle(self, request: Request) -> ResultEnvelope:
    ...
```

流程：

```text
Context
 ↓
PRE_ROUTE
 ↓
Router
 ↓
RouteDecision
 ↓
FAST / PLAN / EXPLORE / HYBRID
```

Direct Invocation 与 execute_plan API 继续稳定存在。

---

# 17. ExplorationEngine

## 17.1 状态

建议：

```text
exploration_id
request_id
mode
step
status
budget
allowed_capabilities
observations
action_history
result
```

## 17.2 Bounded ReAct

不以自由文本 Thought 作为协议。

使用：

```text
Decision
ActionProposal
Observation
Decision
...
```

## 17.3 ActionProposal

```text
capability_id
input
idempotency_key?
reason_code
expected_observation_type?
```

## 17.4 ScopedActionExecutor

执行前检查：

```text
Capability allowed?
Budget allowed?
Deadline?
Policy?
Approval?
Provider available?
Idempotency?
```

然后调用现有 CapabilityInvoker。

---

# 18. Exploration WAITING / Restart

EXPLORE 也必须支持长生命周期场景。

可以复用 Stage 2：

```text
Approval WAITING
Async WAITING
Cancellation
Deadline
```

但需要新增 Exploration checkpoint。

核心原则：

```text
探索进程崩溃
≠
用户取消
```

恢复后从最后一个稳定 Observation / Action 边界继续，而不是重新执行已经确认完成的 WRITE Action。

---

# 19. HYBRID

推荐默认复杂生产模式。

典型：

```text
n1 query
n2 query
     ↓
n3 explore
     ↓
n4 approval
     ↓
n5 report
```

n3 内部：

```text
bounded exploration
```

对 Scheduler 来说，n3 仍表现为一个有明确 ResultEnvelope 的执行节点。

---

# 20. PlanPatchProposal

Explorer 发现需要扩展主 Plan：

```text
“需要额外查询促销变更和支付渠道状态”
```

返回：

```text
PlanPatchProposal
```

不得自己修改 Plan。

限制：

```text
base_revision 必须匹配
不能删除已完成节点
不能修改已完成节点输入语义
新增节点必须通过 PlanValidator
必须重新跑 PRE_PLAN / PRE_PATCH Policy
必须重新计算 Budget
revision 单调递增
```

---

# 21. ConnectorProvider

Stage 3D 最小实现：

```text
QueryConnector
RetrieverConnector
```

目标不是做数据平台，而是证明：

```text
data.query/v1
├── connector-a
└── connector-b
```

可以被 Selector 替换。

---

# 22. MemoryProvider

Stage 3D 最小接口：

```text
load
search
write
compact
```

第一版：

```text
InMemoryMemoryProvider
```

只验证 SPI、Context Slice、Policy 与多 Provider 边界。

真实向量数据库、长期记忆质量策略延后。

---

# 23. Policy

Stage 3 新增建议：

```text
PRE_ROUTE
PRE_PATCH
```

现有：

```text
PRE_PLAN
PRE_EXECUTE
```

Policy 可以：

```text
强制 execution_mode
禁止 EXPLORE
限制 Explorer capability scope
限制 Provider
要求特定 region
禁止 external egress
要求 Approval
```

---

# 24. Trace / Events

## 24.1 Trace

新增：

```text
ROUTE
PROVIDER_SELECT
EXPLORATION
MODEL
CONNECTOR
```

但不要为每个瞬时状态疯狂增加 SpanType；短状态继续用 Event。

## 24.2 Events

至少：

```text
route.decided
mode.selected

provider.candidates
provider.selected
provider.fallback
provider.health_changed

planner.started
planner.repairing
planner.completed
planner.failed

exploration.started
exploration.action_proposed
exploration.action_completed
exploration.waiting
exploration.resumed
exploration.completed
exploration.budget_exhausted

plan_patch.proposed
plan_patch.accepted
plan_patch.rejected
```

---

# 25. Replay Eval

## 25.1 Selection Replay

输入历史：

```text
capability
candidates
selection context
old decision
outcome
```

让新 Selector 重新计算：

```text
would_select
```

不重新执行 Provider。

## 25.2 Route Replay

历史 Request summary：

```text
old route
new router route
```

统计 Route Accuracy / change rate。

## 25.3 Plan Replay

默认：

```text
重新执行 Planner
重新做 PlanValidator
比较 Plan shape / capability selection
```

不自动执行 WRITE 节点。

---

# 26. Error Model

建议新增通用错误码：

```text
HARNESS.PROVIDER.NOT_FOUND
HARNESS.PROVIDER.NO_ELIGIBLE_CANDIDATE
HARNESS.PROVIDER.PIN_NOT_ALLOWED
HARNESS.PROVIDER.FALLBACK_UNSAFE
HARNESS.PROVIDER.HEALTH_UNAVAILABLE

HARNESS.ROUTE.INVALID_DECISION
HARNESS.ROUTE.MODE_NOT_ALLOWED

HARNESS.PLANNER.INVALID_OUTPUT
HARNESS.PLANNER.REPAIR_EXHAUSTED

HARNESS.EXPLORATION.BUDGET_EXHAUSTED
HARNESS.EXPLORATION.ACTION_NOT_ALLOWED
HARNESS.EXPLORATION.RESUME_UNSAFE

HARNESS.PLAN_PATCH.INVALID
HARNESS.PLAN_PATCH.REVISION_CONFLICT
HARNESS.PLAN_PATCH.DENIED
```

---

# 27. 测试策略

## 27.1 Multi Provider

```text
两个 Provider 同 capability 注册
candidate filtering
priority selection
tenant visibility
provider pin
health filtering
canary stable bucket
```

## 27.2 Fallback

```text
READ A fail → B success
retry A before fallback
all providers fail
non-idempotent WRITE fallback rejected
idempotent equivalent WRITE controlled fallback
```

## 27.3 Model

```text
fast model
quality model
model timeout
model fallback
structured output invalid
```

## 27.4 Router

```text
FAST
PLAN
EXPLORE
HYBRID
forced mode
policy denied mode
invalid LLM route
```

## 27.5 Planner

```text
valid first attempt
invalid → repair → valid
repair exhausted
unknown capability
cycle
unsafe write missing approval / policy handling
```

## 27.6 Explore

```text
multi-step success
max_steps
tool_calls exhausted
deadline
approval WAITING
async WAITING
restart
repeated action guard
unsafe write
scope violation
```

## 27.7 Hybrid

```text
Plan → Explore Node → Result → downstream
Explore → PlanPatch → revision → continue
restart during Explore
restart after accepted Patch
```

---

# 28. Stage 3 最终 E2E

建议验收请求：

```text
“调查昨天收入下降原因，必要时继续深挖，
最后生成一份带证据的分析报告。”
```

AUTO：

```text
Router
 ↓
HYBRID
 ↓
LLMPlanner
 ↓
ExecutionPlan

n1 revenue.query
n2 order.query
      │
      └──→ n3 investigate.explore
                    │
                    ├── metrics.query
                    ├── incident.search
                    └── metadata.lineage.read
                              │
                              ▼
                    PlanPatchProposal（必要时）
                              │
                              ▼
                         Plan Revision
                              │
                              ▼
                         n4 Approval
                              │
                              ▼
                         n5 report
```

Provider：

```text
model.generate/v1
├── fast-model
├── quality-model
└── backup-model
```

注入故障：

```text
quality-model transient failure
    ↓
fallback backup-model

explore step waits approval
    ↓
SQLite checkpoint
    ↓
process restart
    ↓
approve
    ↓
resume
```

最终验证：

```text
Result SUCCESS / PARTIAL
完整 provider selection trace
完整 route / plan / exploration trace
无重复 WRITE
Plan revision 可审计
Replay Eval 可复现 route / selection
```

---

# 29. 推荐实施顺序

严格建议：

```text
1. Provider Contracts
      ↓
2. Registry 1:N
      ↓
3. ProviderSelector SPI
      ↓
4. CapabilityInvoker Selection Integration
      ↓
5. Health / Eligibility
      ↓
6. Retry vs Fallback
      ↓
7. Pinning / Canary
      ↓
8. ModelProvider / ModelGateway
      ↓
9. ExecutionMode / RouteDecision
      ↓
10. HarnessApplication.handle()
      ↓
11. RuleRouter
      ↓
12. LLMRouter
      ↓
13. LLMPlanner + bounded repair
      ↓
14. Exploration Contracts
      ↓
15. ExplorationEngine + ScopedActionExecutor
      ↓
16. Explore Checkpoint / Resume
      ↓
17. HYBRID
      ↓
18. PlanPatchProposal / Revision
      ↓
19. ConnectorProvider
      ↓
20. MemoryProvider
      ↓
21. Trace / Events / Metrics completion
      ↓
22. Replay Eval
      ↓
23. E2E / Fault Injection / Restart
```

---

# 30. 验收标准

第三阶段完成至少满足：

1. 同一 capability 可以注册多个 Provider。
2. Planner 仍只面向 capability，不依赖 provider_id。
3. Selector 可以基于 Health / Policy / Cost 等约束选择 Provider。
4. Retry 和 Fallback 有明确不同的可观测语义。
5. 非幂等 WRITE 不会自动跨 Provider fallback。
6. ModelProvider 可替换，Router / Planner 不直接引用厂商 SDK。
7. `handle()` 可以执行 FAST / PLAN / EXPLORE / HYBRID。
8. LLMRouter 只产生 RouteDecision。
9. LLMPlanner 只产生经过 PlanValidator 的 ExecutionPlan。
10. Explore 每次 Action 都经过 ScopedActionExecutor / CapabilityInvoker。
11. Explore 有 steps / calls / deadline / cost / scope / recursion guard。
12. Approval / Async WAITING 在 Explore / Hybrid 中仍可恢复。
13. PlanPatchProposal 不允许直接修改主 Plan。
14. Plan revision 可 checkpoint / audit / resume。
15. Memory 与 StateStore 明确分离。
16. Replay 默认不会重放真实 WRITE 副作用。
17. Stage 1 Direct Invocation 与 Stage 2 execute_plan API 继续兼容。
18. Trace 能关联 Route → Plan/Explore → Provider Select → Capability。
19. Plugin 不获得 Harness 内部 Service Locator。
20. Fault Injection 证明模型失败、Provider 失败、进程重启不会破坏执行一致性。

---

# 31. 架构红线

```text
Router 禁止 execute
Planner 禁止 execute
Explorer 禁止裸调 Provider
Model 禁止修改 StateStore
Model 禁止直接修改主 DAG
ProviderSelector 禁止执行 Provider
Registry 禁止承担业务 Route
Memory 禁止承担 Execution State
Plugin 禁止拿全局 Runtime service locator
WRITE fallback 禁止绕过幂等
```

---

# 32. 需要讨论 / 最终拍板的 ADR

以下内容建议在开始编码前确认。

## OPEN-1：ExecutionMode 放在哪里？

### 方案 A（推荐）

```text
RequestOptions.execution_mode
```

优点：

- API / SDK / Event 都统一；
- 可持久化；
- transport agnostic。

`handle(request, mode=...)` 仅做方便调用的 sugar。

### 方案 B

只放 `handle(..., mode=...)`。

问题：Event/Remote 调用时语义不稳定。

**当前文档按方案 A 设计。**

---

## OPEN-2：ReAct 是 Harness-owned 还是 AgentPlugin-owned？

### 方案 A（推荐 Stage 3）

```text
Harness ExplorationEngine
```

模型只提交 ActionProposal。

优点：

- 最容易保证 Policy / Scope / Approval / Resume；
- 不破坏 Plugin 依赖边界；
- 不需要给普通 AgentPlugin 注入 Runtime。

### 方案 B

新增 `AgenticAgentSPI`，由 Agent 自己循环，但只获得受限 Action Port。

优点：更强的 Agent 自定义能力。

代价：SPI、生命周期、checkpoint 和安全边界复杂很多。

**当前文档 Stage 3 采用 A，把 B 留作后续演进。**

---

## OPEN-3：WorkflowSPI 是否进入 Stage 3？

当前建议：

```text
不进入
```

原因：

- StaticPlanner + ExecutionPlan 已经能表达固定 Workflow；
- 当前更缺 Provider / Router / Planner / Explore；
- Workflow-as-Capability 可在 Stage 4 单独设计版本化和 Nested Workflow。

---

## OPEN-4：ModelProvider 是否统一注册为 Capability？

当前建议采用“两层统一”：

```text
ModelProvider SPI
      ↓
ModelGateway
      ↓
同时暴露 model.generate/v1 等稳定 capability 语义
```

Router / Planner / Explorer 调用 ModelGateway，不直接依赖具体 Provider。

需要继续讨论是否要求所有模型调用也统一经过 CapabilityInvoker，还是由 ModelGateway 复用 Selection / Policy / Trace 组件形成独立平台调用边界。

---

## OPEN-5：WRITE Provider Fallback 的等价判定

推荐必须引入：

```text
provider equivalence_group
+
stable idempotency key
```

没有明确等价声明时：

```text
WRITE 不自动跨 Provider fallback
```

这个规则建议在 Stage 3A 编码前冻结。

---

# 33. 一句话原则

> **Stage 3 不把执行权交给 LLM，而是把 LLM 的路由、规划和探索能力装配到 Stage 2 已经可靠的执行边界之上。**
