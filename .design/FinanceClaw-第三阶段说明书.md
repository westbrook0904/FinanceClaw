# FinanceClaw 第三阶段设计说明书

> **文档性质**：阶段实施设计 / Architecture Decision Baseline  
> **阶段名称**：Stage 3 — Adaptive Multi-Provider & Agentic Orchestration  
> **版本**：V1.3（Foundation F2 完成状态）
> **日期**：2026-09-01
> **前置基线**：Stage 1 Minimal Harness + Stage 2 Reliable Plan Execution Engine  
> **依据文档**：`.design/Harness-Agent_通用可插拔智能体平台架构设计_修订版.md`、`.design/第一阶段.md`、`.design/FinanceClaw-第二阶段说明书.md`
> **Foundation 实施基线**：`.design/FinanceClaw-Agent-Foundation-一期实施说明书.md`
> **当前路线图**：`.design/FinanceClaw-Agent-Foundation-一期路线图.md`

---

# 0. 阶段结论

当前实施状态：

| Milestone | 状态 | 已落地范围 |
|---|---|---|
| Stage 3A — Provider Fabric | 已完成 | Registry 1:N、Selection/Minimal Health、Retry/Fallback、Provider-safe Resume、ModelGateway、Observability |
| Stage 3B — Routing & Planning | 已完成 | ExecutionMode、handle、Rule/LLM Router、PRE_ROUTE、Static/Hybrid/LLM Planner、bounded repair、Acceptance Gate |
| Agent Foundation F0–F2 | 已完成 | Plan identity、Strict Output、Routing correctness 与统一 Context Engineering |
| Agent Foundation F3 | 待实施 | MemoryProvider/Gateway、InMemory/SQLite、Policy 与 Context 接入 |
| Agent Foundation F4–F5 | 待实施 | 再实现最小 standalone EXPLORE，并进入真实业务试用 Gate |
| Post-Foundation Advanced | 设计储备 | HYBRID、PlanPatch、高阶预算、复杂恢复与大规模 Replay，待一期投产后重新 ADR |

第三阶段的目标不是“加一个会自由调用所有工具的 MainAgent”。

第三阶段要完成两个连续升级：

```text
Stage 2:
可插拔 + 可编排 + 可恢复的 Execution Platform

             ↓

Stage 3A:
同一 Capability 的多个 Provider 可选择、可降级、可评测

             ↓

Stage 3B / Foundation:
模型可以在可信 Context 和 Memory 支撑下可靠 Route / Plan / Minimal Explore，
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

第三阶段按 Foundation-first 顺序推进。Stage 3A / 3B 是已完成基线；当前 Foundation 不再把
Context、Memory 与 Explore 拆成前后倒置的两个阶段。

## Stage 3A — Provider Fabric

```text
Provider Identity / Descriptor
Registry 1:N
ProviderSelector
SelectionContext / SelectionDecision
Health / Eligibility
Retry vs Fallback
ModelProvider
Provider Selection Trace / Events
```

Provider Pin 外部入口、Weighted Canary 与 Passive Health 已从 3A 完成门槛移出，等待真实
Provider 运营数据后单独评审，不再由旧 3D 编号自动触发。

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
Route / Plan Eval 稳定事实
```

离线 Replay、准确率统计与策略对比执行器属于投产后的 advanced backlog，不属于已完成的 3B。

## Agent Foundation F0–F1 — 已完成前置与 Routing Correctness

```text
fresh Plan identity
Strict Structured Output
RoutingPipeline deterministic-first
模型只生成未知字段的 Draft / Proposal
requested_mode / effective_mode 不进入模型 Prompt
Router / Planner 共用 strict structured generation adapter
```

## Agent Foundation F2 — Context Engineering（已完成）

```text
ContextItem / ContextSnapshot / ContextProjection
ContextSource / ContextAssembler / ContextProjector
Context Policy / provenance / deterministic trimming
Router / Planner ContextProjection integration
```

## Agent Foundation F3 — Memory

```text
MemoryProvider
MemoryGateway
InMemory / SQLite MemoryProvider
Memory read / write Policy
MemorySlice → ContextAssembler integration
```

Context 与 Memory 必须在 Explore 之前形成公共能力，不能作为 Explore 内部临时 Prompt 拼装逻辑。

## Agent Foundation F4–F5 — Minimal Agent Loop & Real-use Gate

```text
ExplorationEngine
ExplorationBudget
ActionProposal
ScopedActionExecutor
Explore child state in PlanExecutionRecord
standalone EXPLORE mode
真实业务试用与基础质量基线
```

一期只实现串行、单 Action turn、无 Patch 的 standalone EXPLORE。唯一当前实施契约是
`.design/FinanceClaw-Agent-Foundation-一期实施说明书.md`。旧 Stage 3C Agentic Exploration
文档整体作为高阶设计参考，不直接生成编码任务。

Minimal Explore 使用已完成的前置能力：

```text
Planner output 在 Coordinator trust boundary 每次 fresh execution 物化新 plan_id
provider-native strict output + 本地完整校验 + 业务校验
standalone EXPLORE 物化为真实单 EXPLORATION 节点 Plan
HYBRID / PlanPatch 继续 fail-closed
```

Selection/Route/Plan Replay、Provider Comparison 与完整策略优化不再和 Memory 打包，统一进入
一期真实使用后的 advanced backlog。

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

已在 Stage 3B 落地到 `RequestOptions.execution_mode`；`handle(..., mode=...)` 仅为 sugar。
AUTO/FAST/PLAN 可执行；EXPLORE 在 Foundation F4 完成前 fail-closed，HYBRID 在一期投产后重新
ADR 之前始终 fail-closed。

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

## ADR-P3-009：LLM Planner 只产出受控 Plan

LLMPlanner：

```text
Goal + Catalog
      ↓
ModelProvider
      ↓
Structured PlanDraft
      ↓ Harness assigns plan_id / revision
ExecutionPlan
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
Provider Selection
Idempotency
```

一期 Explore 固定 `side_effect ∈ {NONE, READ}`、`egress ∈ {NONE, INTERNAL}` 且同步终结；若执行链
要求 Approval 或返回 Async，则按 Foundation 实施契约 fail-closed / unsafe terminal。

---

## ADR-P3-011：模型没有执行权

模型允许输出 identity-free Draft / Proposal：

```text
Route Proposal
PlanDraft
ExplorationTurnDraft
FinalResultDraft
```

Harness 负责物化最终 `RouteDecision`、`ExecutionPlan`、`ActionProposal` 的控制字段、身份、Scope、
基础次数限制与幂等键。`PlanPatchDraft / PlanPatchProposal` 仅是后续高阶设计储备。

模型禁止：

```text
直接执行 Provider
直接修改 StateStore
直接修改主 DAG
直接授予自己权限
```

---

## ADR-P3-012：PlanPatch 是受治理的 Plan Revision（设计储备）

```text
identity-free PlanPatchDraft
  ↓
Harness materialize Proposal
  ↓
append-only / scope / budget validation
  ↓
PRE_PATCH → PlanValidator → PRE_PLAN
  ↓
CAS 原子保存 same plan_id / revision + 1
  ↓
new Scheduler generation continues from Patch-added tail
```

该方向不进入一期实施。若真实使用证明需要动态扩展主 Plan，必须重新提交 ADR；届时优先沿用
append-only、不可改写历史的安全约束。

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

## ADR-P3-015：Replay 默认不重新执行 WRITE（投产后设计储备）

若投产后启动 Replay Eval，第一版候选为：

```text
Selection Replay
Route Replay
Plan Validation Replay
```

不会重新执行真实副作用节点。

---

# 5. 目标架构

下图包含长期目标态。当前只启用 FAST / PLAN；一期增加 standalone EXPLORE，HYBRID 分支不启用。

```text
                         HarnessApplication.handle()
                                   │
                                   ▼
                             PRE_ROUTE Policy
                                   │
                                   ▼
                     RoutingPipeline
                 deterministic → model fallback
                                   │
                 ┌─────────────────┼─────────────────┐
                 ▼                                   ▼
               FAST                         PLAN / EXPLORE / HYBRID
                 │                                   │
                 │                       Planner artifact /
                 │                       ExplorationPlanFactory
                 │                                   │
                 │                         PlanMaterializer
                 │                                   │
                 │                           ExecutionPlan
                 │                                   │
                 │                       PlanValidator + PRE_PLAN
                 │                                   │
                 │                           ExecutionEngine
                 │                                   │
                 │             ┌───────────┼───────────┐
                 │             ▼           ▼           ▼
                 │       CAPABILITY   APPROVAL   EXPLORATION
                 │             │                       │
                 │             │              ExplorationEngine
                 │             │                       │
                 │             │              ScopedActionExecutor
                 └─────────────┬──────────────────────┘
                               ▼
                        CapabilityInvoker
                               │
                  Registry → ProviderSelector
                               │
                     Agent / Tool / Model
```

standalone EXPLORE 不绕过 Plan / ExecutionEngine；它物化为真实单
EXPLORATION 节点 Plan。图中的 HYBRID 是投产后的候选形态，不属于一期实现。
Exploration child state 与外层 NodeExecutionState 一起写入 PlanExecutionRecord，不建立
独立 Exploration checkpoint 真相。

---

# 6. 新增 / 修改模块

## 6.1 `harness-contracts`

Foundation 当前新增或保留：

```text
ExecutionMode
RouteDecision
ProviderDescriptor
ProviderHealthSnapshot
SelectionContext
SelectionDecision
ProviderAttempt
PlanNodeKind.EXPLORATION
ExplorationNodeSpec
ExplorationBudget
ExplorationUsage
ActionProposal / ActionExecutionState
Observation
ExplorationState
StructuredOutputSpec / Model accounting contracts
ContextItem / ContextSnapshot / ContextProjection / ContextUseRecord
MemoryRecord / MemoryQuery / MemorySlice / MemoryWriteDraft / MemoryWriteProposal
Capability completion mode
```

PlanPatchProposal / PlanPatchExecutionState、PlanRevisionAudit、复杂 ExecutionUnitRef 和跨 worker
恢复状态属于投产后设计储备，不进入一期 Contract。

---

## 6.2 `harness-registry`

修改为：

```text
register provider
unregister provider
list providers
candidates(capability)
get provider by provider_id
immutable ModelProviderFeatures snapshot
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
strict Structured Output preparation + local validation
complete retry/fallback accounting
EmbedRequest / EmbedResult（真实 Memory 检索需要时再评审）
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
RoutingPipeline（deterministic-first）
RouterNotApplicableError
Route Draft / RouteDecision materialization
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
PlanTemplate / PlannerOutputNormalizer / PlanMaterializer
PlanNodeDraft
```

PlanPatch validation 不进入一期 planning 模块。

---

## 6.7 `harness-agentic`（建议新增）

```text
ExplorationEngine
ScopedActionExecutor
ExplorationBudgetGuard
ActionValidator
ExplorationNodeExecutor
```

第一版不新增万能 `AgenticAgentSPI`，也不新增 PlanPatchCoordinator。

---

## 6.8 `harness-execution` / `harness-state`

```text
EXPLORATION node dispatch
minimal ExplorationState
completed Observation boundary recovery
non-completed exploration state fail-closed
```

Exploration 不建立第二个 StateStore；所有 child state 进入同一 PlanExecutionRecord。
operation claim、lease takeover、Plan mutation handoff 与 Patch CAS 属于后续高阶设计。

---

## 6.9 `harness-runtime` / `harness-bootstrap`

```text
Context / Memory / Router / Planner / Explorer 安全组装
FAST / PLAN / EXPLORE / HYBRID 穷举分派（HYBRID fail-closed）
Exploration profile / executor 组装
```

---

## 6.10 `harness-context`（Foundation F2 新增）

```text
ContextSource
ContextAssembler
ContextPolicy
ContextProjector
PromptBuilder
ContextSnapshot / ContextProjection
```

---

## 6.11 `harness-connectors`（可选，真实抽象出现后）

```text
ConnectorProvider SPI
QueryConnector
RetrieverConnector
MockQueryConnector
MockRetrieverConnector
```

一期业务检索可以继续使用现有 Tool / Agent Capability；只有多个真实 connector 出现共同边界后，
才引入专用 ConnectorProvider SPI。

---

## 6.12 `harness-memory`（Foundation F3）

```text
MemoryProvider SPI / MemoryGateway
search
put
delete

InMemoryMemoryProvider
SQLiteMemoryProvider
```

同时实现 namespace、TTL、provenance 与 Policy-gated write；不在一期接真实 Vector DB。

---

## 6.13 `harness-eval`（投产后扩展）

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
RequestOptions(execution_mode=ExecutionMode.AUTO)
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
explorer_id?
confidence?
reason_code
```

Planner ID 不属于模型/Router 决策字段；由服务端配置与 PRE_ROUTE Policy 约束选择。

## 14.2 RuleRouter

已实现 deterministic-first：显式模式、target、input-type rule，最后才进入可选 fallback。

## 14.3 LLMRouter

LLMRouter 只能：

```text
读取安全 Request summary
读取 CapabilityCatalog summary
调用 ModelGateway
让模型返回仅含未知字段的 Route Draft
由 Harness materialize 并返回验证后的 RouteDecision
```

不得执行任何业务 Capability。
模型 Prompt 不携带 requested/effective mode，也不得让模型回显
mode / route_type / source 等 Harness-owned 字段。

Stage 3B 已实现结构化 JSON 输出、独立 RouteDecisionValidator、ModelGateway Retry/Fallback
复用和安全 Request/Catalog 投影。

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
Model output: PlanDraft
Planner output: validated ExecutionPlan with Harness-owned identity
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
async def handle(self, request: Request) -> ResultEnvelope: ...
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

Stage 3B 中只有 AUTO/FAST/PLAN 实际分派；EXPLORE/HYBRID 由 Validator 返回
`HARNESS.ROUTE.MODE_NOT_AVAILABLE`，不得静默降级为 PLAN。

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
reason_code
expected_observation_type?
```

以上是模型可生成的 identity-free Action Draft。正式 ActionProposal 中的
action_id、proposal_hash 与 idempotency_key 都是 Harness-owned materialized fields；
模型 Schema 必须拒绝 idempotency_key。

## 17.4 ScopedActionExecutor

执行前检查：

```text
Capability allowed?
Input schema valid?
Basic count / repeat allowed?
Side effect NONE/READ?
Egress NONE/INTERNAL?
Completion explicitly SYNC?
Deadline?
Provider available?
PRE_EXECUTE Policy ALLOW?
```

ActionProposal checkpoint 后调用现有 CapabilityInvoker。PRE_EXECUTE 要求 Approval 时零 Provider
outbound 并终止本次 Exploration；unexpected ACCEPTED 按 ORPHANED/FAILED 处理。

---

# 18. Exploration Recovery 边界

一期只支持从 completed Observation 边界恢复：该 Observation 与 terminal Action result 已完整
写入同一 PlanExecutionRecord，且 `pending_action_id=None`。

```text
completed Observation → 可以继续下一轮
PROPOSED / RUNNING    → RESUME_UNSAFE
Approval / Async      → 不进入 WAITING，fail-closed
WRITE / egress        → 不进入一期 Action scope
cross-worker takeover → 不支持
```

Stage 2 的 Approval / Async / Resume 能力继续服务既有 CAPABILITY / APPROVAL 节点，但一期不把整套
长生命周期协议复制到 Exploration 子状态。复杂 checkpoint CAS、operation claim 与 takeover 留待
真实部署提出故障模型后再设计。

---

# 19. HYBRID（后续高阶设计储备）

一期不实现，也不作为默认复杂生产模式。只有 FAST / PLAN / standalone EXPLORE 投入使用后，
出现稳定证据证明任务同时需要宏观 DAG 和局部未知探索，才重新评审本节。

典型基础 Plan：

```text
n1 query
n2 query
     ↓
n3 explore barrier
```

n3 内部：

```text
bounded exploration
```

对 Scheduler 来说，n3 仍表现为一个有明确 ResultEnvelope 的执行节点。
如果它接受 Patch，n4 Approval / n5 report 是 Patch 新增的 tail，不是被动态
改写依赖的既有 downstream。

---

# 20. PlanPatchProposal（后续高阶设计储备）

本节不属于一期 backlog。以下内容只保存安全设计方向，不能直接转化为当前实现任务。

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
不能删除或替换任何既有节点 / 边
新 edge 的 to_node 必须是新节点
只追加 new tail 并可更新 final outputs
修改尚未 READY 的既有节点属于 Patch v2，不属于 3C v1
新节点不得越过 persisted scope / side-effect / egress / action / provider-attempt budget
新增节点必须通过 PlanValidator
必须按 PRE_PATCH → PlanValidator → PRE_PLAN → CAS 顺序执行
revision 单调递增
```

---

# 21. ConnectorProvider

一期可选的最小 Context Source 适配：

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

Foundation F3 基础接口：

```text
get
search
put
delete
```

第一版先完成：

```text
InMemoryMemoryProvider
SQLiteMemoryProvider
```

验证 SPI、MemoryGateway、ContextProjection、namespace、Policy、持久化与删除边界。

真实向量数据库、长期记忆质量策略延后。

---

# 23. Policy

Stage 3B 已新增：

```text
PRE_ROUTE
```

后续高阶设计预留，不进入一期：

```text
PRE_PATCH
```

现有：

```text
PRE_PLAN
PRE_EXECUTE
```

Agent Foundation 新增类型化 phase：

```text
PRE_CONTEXT
PRE_MEMORY_READ
PRE_MEMORY_WRITE
PRE_MEMORY_DELETE
```

它们复用同一个 PolicyEngine，基础 guard 始终执行；一期仅接受 ALLOW / DENY，不建立新的 Approval
waiting。

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

既有 PLAN/CAPABILITY 路径继续使用 Stage 2 Approval；最小 Explore 遇到 PRE_EXECUTE
REQUIRE_APPROVAL 时零 Provider outbound 并 fail-closed。

3B 的 PRE_ROUTE 只接受 forced/allowed mode、Capability/Planner scope、Plan attempts/nodes
上限；REQUIRE_APPROVAL 不创建 Request-level waiting，而是 fail-closed。

---

# 24. Trace / Events

## 24.1 Trace

新增：

```text
ROUTE
PLANNER
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
route.failed

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

# 25. Replay Eval（投产后设计储备）

本节不进入一期实现。只有积累足够真实结构化轨迹后，才单独评审 Replay 的数据契约与指标。

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

一期只执行 multi-step success、基础次数限制、repeated action、scope violation 与 completed
Observation recovery。Approval / Async / WRITE / 复杂 restart 条目转入后续 Gate。

```text
multi-step success
max_steps
action_calls exhausted
completed Observation recovery
repeated action guard
scope violation
Approval / Async / WRITE fail-closed
```

## 27.7 Hybrid（后续高阶设计储备）

```text
Plan → Explore Node → Result → downstream
Explore → PlanPatch → revision → continue
restart during Explore
restart after accepted Patch
```

---

# 28. Stage 3 最终 E2E

一期验收改为：

```text
真实 Request
  ↓
ContextAssembler + governed MemorySlice
  ↓
FAST / PLAN / standalone EXPLORE
  ↓
一个同步 READ Action / Observation 循环
  ↓
带 evidence refs 的结果
  ↓
Policy-gated MemoryWriteProposal
```

以下 HYBRID + PlanPatch 流程只保留为后续目标态，不作为当前验收请求。

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
                 Patch-added new tail:
                         n4 Approval
                              │
                              ▼
                         n5 report
```

`n4` / `n5` 只能是 Patch 追加的 new tail；基础 Plan 不预置可被 Explore
改写的 downstream，Patch 也不得向任何既有节点添加入边。

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
完整 route / plan / context / memory / exploration trace
无重复 WRITE
Memory write 具备 evidence / Policy / namespace 记录
HYBRID / PlanPatch 未进入一期运行路径
```

---

# 29. 推荐实施顺序

阶段状态与后续顺序：

```text
Stage 3A — Provider Fabric
  已完成；以 Stage 3A 实施说明书和验收集为准

Stage 3B — Routing & Planning
  已完成；以 Stage 3B 实施说明书和验收集为准

Agent Foundation F1 — Routing Correctness
  已完成 deterministic-first 与模型只填写未知字段

Agent Foundation F2 — Context Engineering
  已完成 Context pipeline、Router/Planner Projection、稳定 hash 与安全裁剪

Agent Foundation F3 — Memory
  下一步完成 MemoryProvider / Gateway、InMemory / SQLite 与 Context 接入

Agent Foundation F4–F5 — Minimal Explore & Real-use Gate
  再实施最小 standalone EXPLORE、安全 Action、基础次数限制与真实试用

Post-Foundation Advanced
  一期投产后根据真实问题重新评审 HYBRID、PlanPatch、高阶预算与 Replay
```

当前步骤与 Gate 以 `FinanceClaw-Agent-Foundation-一期路线图.md` 和
`FinanceClaw-Agent-Foundation-一期实施说明书.md` 为基线。旧 Stage 3C Agentic Exploration
文档整体仅作设计储备。

---

# 30. 验收标准

第三阶段完成至少满足：

1. 同一 capability 可以注册多个 Provider。
2. Planner 仍只面向 capability，不依赖 provider_id。
3. Selector 可以基于 Health / Policy / 显式资格约束选择 Provider。
4. Retry 和 Fallback 有明确不同的可观测语义。
5. 非幂等 WRITE 不会自动跨 Provider fallback。
6. ModelProvider 可替换，Router / Planner 不直接引用厂商 SDK。
7. `handle()` 可以执行 FAST / PLAN / standalone EXPLORE；HYBRID 保持 fail-closed。
8. LLMRouter 边界只返回 Harness 验证后的 RouteDecision；模型只生成
   identity-free Route Draft。
9. 任意 Planner 输出在 Coordinator trust boundary 统一归一化为模板，
   每次 fresh execution 只 materialize 一次新 plan_id，并通过 PlanValidator。
10. Explore 每次 Action 都经过 ScopedActionExecutor / CapabilityInvoker。
11. Explore 有 steps / model calls / action calls / scope / repeat 基础 guard，并固定禁止 nested explore。
12. standalone Explore 只从已完成 Observation 边界恢复；其他中间态 fail-closed。
13. Context projection 具备 provenance、隔离、确定性裁剪和泄漏测试。
14. Memory 具备 namespace、Policy、持久化、删除和过期能力。
15. Memory 与 StateStore 明确分离，Router / Planner / Explorer 只消费受控 ContextProjection。
16. 至少一个真实业务 Agent 场景完成试用并形成质量与失败基线。
17. Stage 1 Direct Invocation 与 Stage 2 execute_plan API 继续兼容。
18. Trace 能关联 Context / Memory → Route → Plan/Explore → Provider Select → Capability。
19. Plugin 不获得 Harness 内部 Service Locator。
20. Fault Injection 证明模型失败、Provider 失败、既有 Plan 恢复与 completed-Observation 恢复不会
    破坏执行一致性；其他 Explore 中间态稳定 fail-closed。

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

# 32. ADR 决议状态

Stage 3A / 3B 编码前的决议已经冻结；当前按 Agent Foundation F1→F5 推进。旧 Stage 3C/3D
编号及高阶条目不再代表实施顺序。

## RESOLVED-1：ExecutionMode 放在哪里？

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

**Stage 3B 已按方案 A 实现。**

---

## RESOLVED-2：ReAct 是 Harness-owned 还是 AgentPlugin-owned？

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

**一期最小 Agent Loop 采用 A；HYBRID、多 Agent 与 AgenticAgentSPI 留待真实使用后评审。**

---

## RESOLVED-3：WorkflowSPI 是否进入 Stage 3？

当前建议：

```text
不进入
```

原因：

- StaticPlanner + ExecutionPlan 已经能表达固定 Workflow；
- 当前更缺 Provider / Router / Planner / Explore；
- Workflow-as-Capability 可在 Stage 4 单独设计版本化和 Nested Workflow。

**Stage 3B 已采用 StaticPlanner / HybridPlanner + ExecutionPlan，不引入 WorkflowSPI。**

---

## RESOLVED-4：ModelProvider 的注册与调用边界

Stage 3A 已采用“两层统一”：

```text
ModelProvider SPI
      ↓
ModelGateway
      ↓
同时暴露 model.generate/v1 等稳定 capability 语义
```

Router / Planner / Explorer 调用 ModelGateway，不直接依赖具体 Provider。ModelProvider 以
`CapabilityType.MODEL` 注册到共享 Registry；ModelGateway 复用 Selection/Minimal Health、
ProviderExecutionCoordinator、Trace 和 Events，但不经过 CapabilityInvoker。模型调用保留
GenerateRequest、structured output、usage、finish reason 和模型参数等原生语义。

---

## RESOLVED-5：WRITE Provider Fallback 的等价判定

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

Stage 3A 已按此规则实现：缺少稳定 idempotency key 或相同非空 equivalence group 时
fail-closed。

---

# 33. 一句话原则

> **Stage 3 不把执行权交给 LLM，而是把 LLM 的路由、规划和探索能力装配到 Stage 2 已经可靠的执行边界之上。**
