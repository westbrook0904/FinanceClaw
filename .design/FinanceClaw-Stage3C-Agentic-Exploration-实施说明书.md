# FinanceClaw Stage 3C 实施说明书

> **文档性质**：Stage 3C 编码实施基线 / Architecture Decision Baseline
> **阶段名称**：Stage 3C — Governed Agentic Exploration
> **版本**：V1.0（设计冻结，尚未实施）
> **日期**：2026-08-29
> **前置基线**：Stage 3A Provider Fabric + Stage 3B Routing & Planning
> **依据文档**：FinanceClaw 第三阶段设计说明书、Stage 3A 实施说明书、Stage 3B 实施说明书
> **阶段边界**：本说明书只覆盖 3C；Connector / Memory / Replay Eval 仍属于 3D，Workflow Catalog 仍属于 Stage 4

---

## 1. 3C 要解决什么

Stage 3A 已经解决：

~~~text
一个 Capability
      ↓
多个 Provider
      ↓
Selection / Retry / Fallback / Resume
~~~

Stage 3B 已经解决：

~~~text
Request
  ↓
PRE_ROUTE
  ↓
Rule / LLM Route
  ↓
FAST 或 PLAN
  ↓
受控 Capability Invocation 或受验证 ExecutionPlan
~~~

Stage 3C 要解决的是：

~~~text
当任务无法在路由时一次性确定完整执行步骤，
如何允许模型逐步观察、提出下一步动作、必要时扩展计划，
同时仍然保持 Scope、Policy、Budget、Checkpoint、Resume、
Provider Safety 和副作用一致性。
~~~

Stage 3C 不是给模型一组 Tool 然后让它自由循环。

Stage 3C 的目标是把探索表达成 Harness-owned、可恢复、可审计、有限状态的执行过程：

~~~text
Model proposes
      ↓
Harness validates
      ↓
Harness checkpoints
      ↓
Harness executes through CapabilityInvoker
      ↓
Harness records bounded Observation
      ↓
Model proposes again
~~~

一句话：

> **模型可以决定下一步建议做什么，但不能决定自己拥有什么权限、如何执行、如何恢复，也不能直接修改主 Plan。**

### 1.1 3C 完成后的核心能力

~~~text
EXPLORE / HYBRID 实际可执行
Harness-owned ExplorationEngine
Bounded structured exploration loop
ActionProposal / Observation
ScopedActionExecutor
Checkpoint-before-dispatch
Approval / Async WAITING / Restart
Explicit EXPLORATION Plan node
Append-only PlanPatchProposal
Plan revision CAS checkpoint
Deterministic-first RoutingPipeline
Model-only-unknown-fields protocol
Strict structured output contract
Fresh plan identity materialization
~~~

### 1.2 3C 的实施优先级

优先级必须是：

~~~text
Boundedness
    ↓
Checkpoint before Action
    ↓
Scope / Policy
    ↓
WRITE / Provider Resume Safety
    ↓
Patch Atomicity
    ↓
Model Flexibility
~~~

不能为了让 Demo 看起来更“自主”，牺牲恢复一致性或治理边界。

---

## 2. 范围与非目标

### 2.1 本阶段必须实现

1. 对 3B 已知设计债务做前置收口：
   - StaticPlanner 模板每次执行必须生成新的 plan_id；
   - Router 固化 deterministic-first 组合语义；
   - LLMRouter 不再让模型回显 mode / route_type / source；
   - requested_mode / effective_mode 不进入模型 Prompt；
   - ModelGateway 增加严格 Structured Output 契约与完整本地校验；
   - Coordinator 对 FAST / PLAN / EXPLORE / HYBRID 做穷举分派。
2. 新增 Harness-owned ExplorationEngine。
3. 新增显式有限的 ExplorationProfile、Scope、Budget 与 Usage。
4. 模型只输出结构化 ExplorationTurnDraft。
5. Harness 物化 ActionProposal、action_id 与可信幂等键。
6. 每个动作只能经 ScopedActionExecutor → CapabilityInvoker。
7. 动作执行前必须 checkpoint proposal 与执行身份。
8. EXPLORE 物化为真实的单 EXPLORATION 节点 ExecutionPlan。
9. HYBRID 使用包含 EXPLORATION 节点的 ExecutionPlan。
10. Exploration 子状态进入 PlanExecutionRecord，复用现有 StateStore 作为唯一执行真相。
11. Approval、Async completion、Cancellation、Deadline、Crash/Resume 支持 action 粒度。
12. 新增 PRE_PATCH Policy。
13. 新增 append-only PlanPatchProposal、PlanPatchValidator、PlanPatchCoordinator。
14. PlanPatch 保持 plan_id，不改变历史执行事实，只让 revision 单调加一。
15. 内置 StateStore 提供 compare-and-save，用于 Patch 原子版本更新。
16. 新增 Explore / Action / Patch Trace、Events 与 Stage 3C Acceptance Gate。

### 2.2 3C 前置收口不是对 3B 语义的推翻

以下调整是 3B 契约的精确化：

~~~text
RouteDecision 仍是 Harness 最终路由协议
Planner SPI 仍返回 ExecutionPlan
invoke / execute_plan / resume_plan 仍保留
RuleRouter 仍 deterministic-first
ModelGateway 仍是统一模型边界
StateStore 仍是执行恢复真相
~~~

变化只发生在“谁生成哪些字段”和“Composition Root 如何保证安全顺序”：

~~~text
3B 当前：
模型生成完整 RouteDecision

3C 收口：
模型生成最小 Route Proposal
Harness 生成最终 RouteDecision
~~~

~~~text
3B 当前：
StaticPlanner 可以原样返回一个 ExecutionPlan 模板

3C 收口：
模板没有运行身份
每次 fresh execution 由 PlanMaterializer 分配新 plan_id
~~~

### 2.3 本阶段明确不实现

~~~text
× ConnectorProvider
× MemoryProvider
× Selection / Route / Plan Replay 执行器
× 真实 OpenAI / Anthropic / Gemini SDK 适配器的完整矩阵
× WorkflowSPI / Workflow Catalog
× 自动把成功 Plan 发布为 Workflow
× AgentPlugin-owned ReAct
× 普通插件获得 CapabilityInvoker / Registry / StateStore
× 并行多 Action turn
× 无限递归或 Nested Exploration
× 模型直接提供可信 idempotency key
× 模型直接选择 Provider / Plugin / Planner / Explorer
× 模型直接修改 PlanExecutionState
× 模型自由删除或替换主 Plan 节点
× PRE_ROUTE Request-level Approval waiting
× 分布式 Scheduler / Lock / Lease
× Remote Plugin / Worker
× 隐藏 Chain-of-Thought 持久化
~~~

### 2.4 与 3D / Stage 4 的边界

3C 只记录未来评测需要的安全事实：

~~~text
route proposal kind
schema / prompt version
plan shape hash
template hash
action proposal hash
observation hash
patch hash
outcome / error code
token / call / latency counters
~~~

3D 才实现：

~~~text
DecisionRecord / EvalCase
Selection Replay
Route Replay
Plan Replay
Provider Comparison
通用执行事实的离线聚类 / 评分 / Eval
~~~

3D 可以产出“某类执行形态值得后续沉淀”的离线证据，但不新建
WorkflowCandidate 持久化实体，不负责参数化、审批流程或发布。

Stage 4 才实现：

~~~text
WorkflowCandidate lifecycle
WorkflowDefinition
WorkflowVersion
Workflow Catalog
Publish / Canary / Rollback
Workflow-as-Capability
Nested Workflow
~~~

plan_id 不能被复用为 workflow_id。

---

## 3. 编码前必须冻结的 14 个设计决定

### 3.1 handle 是标准入口，低层 API 不删除

公开入口分层：

| API | 定位 | 是否 Route / Plan | 典型调用方 |
|---|---|---:|---|
| handle(request) | 标准 Orchestration API | 是 | 普通业务入口 |
| invoke(request) | Direct Capability 高级入口 | 否 | 明确指定 Capability 的系统集成 |
| execute_plan(request, plan) | Prebuilt Plan 高级入口 | 不规划，只验证执行 | 固定流程、测试、未来 Workflow materialization |
| resume_plan(plan_id) | 恢复稳定执行实例 | 否 | WAITING / Restart |

3C 不删除或改变 invoke / execute_plan 的核心语义。

所有文档和示例必须把 handle 标为推荐入口，把其他入口标为 advanced API。

新增 Exploration 后仍优先复用现有 plan 控制 API，不创建一套彼此割裂的
resume_exploration / approve_exploration / complete_exploration API。

### 3.2 deterministic-first 是 RoutingPipeline 不变量

当前 RuleRouter 内部顺序正确，但 Composition Root 允许直接替换整个 Router。

3C 新增明确组合：

~~~text
RoutingPipeline
  ├── primary: deterministic Router
  └── fallback: optional model Router
~~~

只有 primary **抛出具体类型 `RouterNotApplicableError`** 时才允许调用 fallback。
它的稳定 wire code 为 `HARNESS.ROUTE.NOT_APPLICABLE`。普通
`RoutingError`（即使 code 或 message 看起来像 NO_MATCH）也不得触发 fallback。

以下情况禁止调用模型 fallback：

~~~text
static decision schema invalid
Policy denied
fixed mode unavailable
explicit target invalid
deadline / cancellation
unexpected Router exception
Validator failure
~~~

模型 Router 失败后也不能反向猜测确定性结果。

默认 build_harness 仍不依赖模型：

~~~text
default primary = RuleRouter
default fallback = none
~~~

旧 router= 高级覆盖入口保留一个兼容周期，但必须在 API 文档中标明它会显式接管整个 Pipeline。

3B 现有 `RuleRouter(fallback=...)` 不能直接作为 Pipeline primary，否则会在内部
绕过上述类型门禁。3C 迁移规则冻结为：

1. RuleRouter 的新核心路径只做确定性规则匹配，未命中抛
   RouterNotApplicableError；
2. legacy `fallback=` 参数保留一个兼容期，但 Composition Root 必须把它拆成
   `RoutingPipeline(primary=rule_router.deterministic_view(), fallback=legacy_fallback)`；
3. RoutingPipeline 构造时拒绝任何声明 `has_internal_fallback=true` 的 primary；
4. 高级 `router=` 若传入一个完整自定义 Router，它是显式接管 Pipeline，不再
   被包装为 deterministic primary。

阻断 Gate 必须覆盖 legacy RuleRouter(fallback=model) 装配：静态命中时模型
调用为 0，未命中也只有一层 Pipeline fallback。

### 3.3 模型只填写未知字段

ExecutionMode 是控制面事实，不是模型选择 Capability 所需的业务特征。

固定 FAST 且缺少 target 时，模型只选择：

~~~text
capability_id
confidence
reason_code
~~~

固定 PLAN / EXPLORE / HYBRID 时，Router 不调用模型决定 mode。

AUTO 且 deterministic Router 未匹配时，模型返回判别联合：

~~~text
DirectRouteDraft(capability_id)
PlanRouteDraft()
ExploreRouteDraft()
HybridRouteDraft()
~~~

Harness 的 RouteDecisionMaterializer 再补充：

~~~text
mode
route_type
source = model
configured explorer_id
~~~

Prompt 不携带：

~~~text
requested_mode
effective_mode
Policy 原因
provider_id
plugin_id
planner_id
explorer_id
~~~

模型只看到已过滤的可选集合和它需要填写的 Draft Schema。

### 3.4 Structured Output 使用“原生约束 + 本地校验 + 业务校验”

不能把“模型通常会输出正确 JSON”当成可靠协议。

3C 引入：

~~~text
StructuredOutputSpec
ModelProviderFeatures
Strictness
UnsupportedBehavior
~~~

标准链路：

~~~text
Provider-native schema constrained output
        ↓
ModelGateway full local JSON Schema validation
        ↓
Pydantic Draft validation
        ↓
Route / Plan / Action / Patch semantic validator
~~~

STRICT_REQUIRED 不允许静默退化到普通 JSON 或文本。

旧 GenerateRequest.response_format=JSON + response_schema 保留为 legacy best-effort 兼容；
LLMRouter、LLMPlanner、ExplorationEngine 的新版本默认使用 strict contract。

3C 实现 Provider-neutral 契约、能力协商、完整本地校验和 Mock Provider Gate。
具体厂商 SDK adapter 可以在 3D 扩展，但不得再新增一个与 ModelProvider 重复的公共
ModelConnector SPI。

模型拒答、max tokens 截断、content filter、输出不完整都必须映射为失败，不能因为残片碰巧可解析就接受。

### 3.5 plan_id 是 fresh execution identity

冻结语义：

~~~text
plan_id
  = 一次 fresh execution lineage 的稳定身份

revision
  = 同一次 execution 中 Plan 定义的单调版本

workflow_id / workflow_version
  = 未来可复用定义的身份
~~~

规则：

1. 每次新执行必须生成新的 plan_id。
2. resume 保持原 plan_id 和 revision。
3. Patch 保持原 plan_id，revision 精确加一。
4. 两次内容完全相同的计划也必须有不同 plan_id。
5. execute_plan 接收的是具体执行实例；调用方重复提交相同 plan_id 时仍应冲突。
6. 任意 Planner（包括第三方 Planner）的输出都不是最终 execution identity。
7. `handle()` 的 PLAN / HYBRID 路径必须在唯一 trust boundary 丢弃 Planner
   输出中的 plan_id / revision，然后仅 materialize 一次。
8. `execute_plan()` 是明确例外：它接收具体执行实例，不更换身份，重复 ID 冲突时
   fail-closed。

新增 Harness-owned：

~~~text
PlanTemplate（正式 identity-free Contract）
PlanIdentityFactory
PlanMaterializer
PlannerOutputNormalizer
~~~

Planner SPI 对外仍返回 ExecutionPlan，避免立即破坏现有扩展。但该值在
`handle()` 规划路径上只是“带 legacy identity 字段的 Plan candidate”。
PlannerOutputNormalizer 先把它转换为 PlanTemplate，PlanMaterializer 再由 RequestCoordinator
调用一次并且只调用一次。内置 StaticPlanner / LLMPlanner 不再拥有最终 plan_id
生成权；即使自定义 Planner 每次返回同一对象，两次 fresh execution 也必须得到
不同 plan_id。

为避免强迫新内置 Planner 制造 throwaway ExecutionPlan identity，Planner base 新增一个
有 concrete compatibility default 的 additive 方法：

~~~python
async def plan_artifact(
    self,
    context: PlanningContext,
) -> PlanTemplate | ExecutionPlan:
    return await self.plan(context)  # legacy custom Planner default
~~~

RequestCoordinator / PlannerGateway 改为调用 `plan_artifact()`。内置 StaticPlanner /
LLMPlanner 覆写它并直接返回 PlanTemplate；旧自定义 Planner 不用修改，默认
返回的 ExecutionPlan 会被 Normalizer 剥离 identity。旧 `plan()` 方法仍保留签名，
但其直接返回值不能绕过 Coordinator 当成 fresh handle execution 的最终身份。

### 3.6 ExplorationEngine 必须 Harness-owned

普通 AgentPlugin、ToolPlugin、模型 Provider 都不能得到：

~~~text
CapabilityInvoker
Registry
ProviderSelector
ExecutionEngine
StateStore
PolicyEngine
Application service locator
~~~

只有 ScopedActionExecutor 持有 CapabilityInvoker。

模型只提交 Draft；Harness 负责：

~~~text
materialize identity
validate schema
validate scope
validate budget
checkpoint
invoke
observe
resume
~~~

不新增万能 AgenticAgentSPI。

### 3.7 standalone EXPLORE 物化为真实单节点 Plan

3C 不建立第二套 Request-level execution truth。

standalone EXPLORE 的执行方式：

~~~text
EXPLORE RouteDecision
       ↓
ExplorationPlanFactory
       ↓
ExecutionPlan(
  fresh plan_id,
  node kind = EXPLORATION
)
       ↓
ExecutionEngine
~~~

这不是把 exploration_id 冒充 plan_id，也不是给 Invoker 塞伪 context。

它是一份真实、可验证、可 checkpoint 的 ExecutionPlan，因此可以复用：

~~~text
StateStore
resume_plan
resolve_approval
complete_async_node
cancel_plan
Plan / Node Trace
~~~

Exploration action 仍需要独立的 exploration_id / action_id 子状态；不能只靠外层 plan/node 状态。

### 3.8 Action 必须 proposal-before-dispatch

每个动作遵循 write-ahead 语义：

~~~text
model draft
  ↓
validate + materialize action_id
  ↓
checkpoint PROPOSED action
  ↓
dispatch through CapabilityInvoker
  ↓
checkpoint result / waiting
  ↓
build Observation
~~~

崩溃发生在 proposal checkpoint 之后时：

- 恢复同一 Action，不重新询问模型换一个动作；
- 已完成动作不重复执行；
- READ / NONE 按 Provider resume 事实安全恢复；
- WRITE 继续要求稳定 Harness-owned idempotency key 与 3A Provider safety；
- 无法证明安全时返回 HARNESS.EXPLORATION.RESUME_UNSAFE。

模型输出中的任意 idempotency_key 字段必须被 Schema 拒绝。

### 3.9 Budget 与 Usage 分离且 resume 不重置

ExplorationBudget 是不可放宽的上限。

ExplorationUsage 是持久化、单调增长的实际消耗。

至少强制：

~~~text
max_steps
max_model_calls
max_action_calls
max_total_tokens
deadline_at
max_repeated_actions
max_patch_count
max_exploration_depth
max_observations
~~~

第一版：

~~~text
max_exploration_depth = 1
每个 turn 最多一个 Action
每个 Exploration 最多一个 in-flight Action
~~~

如果配置 cost limit，而 Provider 不能提供规范化成本：

~~~text
fail closed
HARNESS.EXPLORATION.BUDGET_ACCOUNTING_UNAVAILABLE
~~~

不能把未知成本按 0 处理。

### 3.10 WAITING 必须定位到 action

外层 ExecutionPlan 提供 plan_id / node_id。

内层 Exploration 提供 exploration_id / action_id。

新增判别 ExecutionUnitRef：

~~~text
PlanNodeRef(plan_id, node_id)

ExplorationActionRef(
  plan_id,
  node_id,
  exploration_id,
  action_id
)

PlanPatchRef(
  plan_id,
  node_id,
  exploration_id,
  patch_id
)
~~~

Continuation、ApprovalRequest、ApprovalGrant 增加可选 execution_ref。
ApprovalRequest 和 ApprovalGrant 同时增加一级类型字段
`proposal_hash: NonEmptyString | None`，不得放在 metadata。

旧 plan_id / node_id wire 字段继续保留，并与 execution_ref 做一致性校验。

Approval Grant 必须绑定具体 action_id 或 patch_id 及 proposal hash，不能授权后续其他动作。
PlanNodeRef 的 legacy Approval 可以同时缺少 execution_ref / proposal_hash；
ExplorationActionRef / PlanPatchRef 必须两者同时存在。

complete_async_node 保留旧 positional 参数，并增加可选 execution_ref / expected_job_ref。
CAPABILITY node 可以继续省略；EXPLORATION node 必须提供并匹配 action ref 与 job_ref。
遇到 EXPLORATION node 时，不得把整个节点直接标记完成；必须先把 terminal result 写入指定
pending ActionState，形成 Observation，再恢复 Exploration loop。仅靠“当前唯一 pending
action”不能抵御上一轮迟到或重复的 callback。

### 3.11 HYBRID 使用显式 EXPLORATION 节点

新增：

~~~text
PlanNodeKind.EXPLORATION
ExplorationNodeSpec
ExplorationNodeExecutor
~~~

EXPLORATION 不是 Registry Capability。

Scheduler 遇到它时：

~~~text
resolve bounded goal input
  ↓
delegate ExplorationNodeExecutor
  ↓
ExplorationEngine
  ↓
ResultEnvelope / WAITING / Patch directive
~~~

LLM PlanDraft 只能表达：

~~~text
需要一个 exploration node
该节点的 goal bindings
请求的 capability subset
建议的较小 budget
~~~

Harness / Policy 注入：

~~~text
exploration_profile_id
最终 scope
最终 budget 上限
exploration_id
allow_patch
~~~

现有 HybridPlanner 是“Planner fallback 组合”，不等于 ExecutionMode.HYBRID。

### 3.12 PlanPatch v1 使用 append-only + CAS

模型只输出 identity-free PlanPatchDraft。

Harness 物化：

~~~text
patch_id
plan_id
base_revision
source_exploration_id
proposal_hash
~~~

v1 允许：

~~~text
add CAPABILITY / APPROVAL nodes
add edges（to_node 必须是新节点；from_node 可以是既有节点或新节点）
add / replace final output bindings
~~~

v1 禁止：

~~~text
remove existing node
replace existing node
remove / replace existing edge
add incoming edge to existing node
modify running / waiting / terminal node definition
modify historical NodeExecutionState
modify provider history / result / approval history
add MODEL-backed Capability or another EXPLORATION node
add WRITE / external-egress Capability in Patch v1
increase deadline / concurrency / token / cost ceiling
expand capability scope without Policy
~~~

Patch 不能用新 Plan node 绕过 Exploration action budget。CAS 前必须物化
`PlanPatchBudgetReservation`：

~~~text
added_node_count <= trusted max_patch_nodes
reserved_action_calls = count(added CAPABILITY nodes)
reserved_action_calls <= ExplorationBudget.max_action_calls - ExplorationUsage.action_calls
every added node retry_policy.max_attempts = 1
finite max_provider_attempts_per_node from trusted Patch execution profile
reserved_provider_attempts = capability_node_count * max_provider_attempts_per_node
deadline_at <= persisted Exploration deadline
~~~

CAS 把 Plan revision、Patch reservation 与 `ExplorationUsage.action_calls += reserved_action_calls`
原子写入，额度预留后不返还；同时为每个新增节点写入 Harness-owned
`patch_node_origins[node_id]`，绑定 patch_id / proposal_hash / descriptor security hash / source
scope hash / ledger / absolute deadline。新 Scheduler 对 Patch-added node 强制共享
`PlanPatchExecutionLedger`，每次 Provider outbound 前 CAS 同时递增总数与该 node 的
consumed_provider_attempts，超限时禁止调用。

Patch-added node admission 以及每次 Provider retry / fallback outbound 都必须重新检查
`now < reservation.deadline_at`，并把实际 timeout 取 request / plan / node timeout 与该绝对
deadline 的最小值；过期时 outbound=0。每次 dispatch 还要从 persisted accepted Patch 解析
origin，并重验当前 Catalog descriptor 的 capability type、side-effect、egress 与 security hash
仍满足 accept 时的 source scope 和 Patch v1 禁令。descriptor 漂移时 fail-closed，不能退回普通
Plan node 路径。

Exploration 的 token/cost ceiling 在 3C 只计量 ModelGateway generation。Patch v1 因此
禁止 MODEL Capability 与嵌套 EXPLORATION；对业务 Capability 内部不可见的费用不做
“已精确计费”的虚假声明。通用 Plan token/cost ledger 属于后续预算扩展；
3C v1 的硬边界是 action count、provider attempt count、node count 与 absolute deadline。

PlanPatchValidator 对每个新 CAPABILITY node 重用 ActionValidator 的 capability type、
persisted scope、side-effect、egress、input schema 与幂等性检查。即使同一 capability_id
已在 scope 中，WRITE 或 external-egress descriptor 在 Patch v1 也必须零 mutation 拒绝。

应用顺序：

~~~text
base revision / state version check
  ↓
append-only structure / scope / budget / depth guard
  ↓
PRE_PATCH
  ↓
materialize candidate revision + 1
  ↓
PlanValidator
  ↓
PRE_PLAN on revised Plan
  ↓
PlanCheckpointCoordinator compare-and-save
  ↓
reload persisted revision / new Scheduler generation
~~~

Plan 与 PlanExecutionState 必须作为一个 PlanExecutionRecord 原子保存。

内置 InMemory / SQLite Store 必须实现 compare-and-save。
第三方 Store 不支持 CAS 时，普通 PLAN 仍可工作，但 PlanPatch 必须 fail-closed。

### 3.13 不持久化隐藏推理

可以持久化：

~~~text
decision kind
decision_summary（有界）
reason_code
proposal hash
selected capability
bounded Observation summary
evidence refs
usage counters
structured validation codes
~~~

禁止持久化到 State / Trace / Events：

~~~text
hidden Chain-of-Thought
raw Prompt
raw model response
credentials
provider raw exception
unbounded business payload
~~~

StateStore 可以保存恢复所需的受控 ResultEnvelope。
Trace / Events 只保存安全 ID、hash、计数与固定摘要。

### 3.14 Workflow 自动晋升不进入 3C

plan_id 只用于执行实例关联，不是 Workflow identity。

3C 可以记录：

~~~text
template_hash
plan_shape_hash
provenance
outcome
cost / token / latency
policy / validation result
~~~

但不能：

~~~text
一次成功
  ↓
自动发布 Workflow
~~~

长期演进闭环应当是：

~~~text
执行事实
  ↓
脱敏 / 聚类 / 离线评分（3D）
  ↓
Workflow Candidate / 参数化（Stage 4）
  ↓
Policy / 人工审核
  ↓
Stage 4 Versioned Publish / Canary / Rollback
~~~

---

## 4. 目标架构

~~~text
                         HarnessApplication.handle()
                                   │
                                   ▼
                             PRE_ROUTE Policy
                                   │
                                   ▼
                          RoutingPipeline
                    deterministic │ model fallback
                                   ▼
                         validated RouteDecision
             ┌─────────────────────┼──────────────────────┐
             ▼                     ▼                      ▼
           FAST                  PLAN              EXPLORE / HYBRID
             │                     │                      │
             ▼                     ▼                      ▼
      CapabilityInvoker          Planner        ExplorationPlanFactory /
                                   │             Hybrid-capable Planner
                                   └──────────────┬───────┘
                                                  ▼
                                           PlanMaterializer
                                                  │
                                                  ▼
                                           ExecutionEngine
                                                  │
                 ┌────────────────────────────────┼─────────────────────┐
                 ▼                                ▼                     ▼
          CAPABILITY node                  APPROVAL node        EXPLORATION node
                 │                                                      │
                 ▼                                                      ▼
       CapabilityInvoker                                      ExplorationNodeExecutor
                                                                        │
                                                                        ▼
                                                               ExplorationEngine
                                                                        │
                                                    ┌───────────────────┼──────────────┐
                                                    ▼                   ▼              ▼
                                              Action Draft         Final Draft    Patch Draft
                                                    │                                  │
                                                    ▼                                  ▼
                                         ScopedActionExecutor                 PlanPatchCoordinator
                                                    │                                  │
                                                    ▼                                  ▼
                                           CapabilityInvoker               PRE_PATCH + CAS
~~~

统一执行真相：

~~~text
PlanExecutionRecord
  ├── immutable ExecutionPlan revision
  ├── InvocationContext
  └── PlanExecutionState
        ├── NodeExecutionState
        ├── ExplorationState by node
        ├── pending approvals / jobs
        └── PlanRevisionAudit
~~~

---

## 5. 关键状态机

### 5.1 Exploration 状态

~~~text
CREATED
   ↓
RUNNING
   ├── WAITING ── approval / async ──→ RUNNING
   ├── SUCCEEDED
   ├── PARTIAL
   ├── FAILED
   ├── DENIED
   └── CANCELLED
~~~

WAITING 原因单独记录：

~~~text
approval
async
patch_approval
~~~

Budget exhausted 不是新增终态：

- 已有可靠 Observation 时返回 PARTIAL + issue；
- 没有可靠结果时返回 FAILED + BUDGET_EXHAUSTED。

### 5.2 Action 状态

~~~text
PROPOSED
   ↓ checkpoint
RUNNING
   ├── WAITING(approval) ── matching Grant ──→ RUNNING
   ├── WAITING(async) ── terminal callback ──→ SUCCEEDED / FAILED / DENIED / CANCELLED
   ├── SUCCEEDED
   ├── FAILED
   ├── DENIED
   └── CANCELLED
~~~

VALIDATED 是瞬时事实，通过 Event 记录，不单独持久化为状态。

Action 终态且 `result != None` / `observation_id == None` 是唯一合法的
`OBSERVATION_PENDING` 过渡形态。此时 pending_action_id 仍指向该 Action；ResumeCoordinator
只做确定性 Observation projection 与 CAS，不重调 Provider。Observation checkpoint
必须同时填写 observation_id 并清除 pending_action_id。

### 5.3 Patch 状态

~~~text
DRAFT
  ↓ materialize
PROPOSED
  ├── REJECTED（invalid / Policy deny / human reject）
  ├── WAITING_APPROVAL
  ├── CONFLICTED（revision / CAS conflict）
  ├── CANCELLED
  └── ACCEPTED
          ↓
     plan revision + 1
~~~

该状态机必须由 PlanPatchExecutionState 持久化，pending_patch_id 只作为索引，不能替代
Proposal、状态、approval、base revision 与 expected state version。

### 5.4 稳定 Checkpoint 边界

必须 checkpoint：

1. Exploration 创建；
2. 模型调用额度预占与 attempt started；
3. 模型 generation 完成后的安全 attempt fact / usage；
4. ActionProposal materialize 后、执行前；
5. Provider attempt start / complete；
6. Action WAITING；
7. Action terminal result；
8. Observation 创建；
9. PatchProposal 创建；
10. Patch approval waiting；
11. Patch CAS 接受；
12. Exploration terminal + outer EXPLORATION Node terminal 同一 CAS checkpoint。

模型调用本身没有业务副作用。若在尚未得到合法 Draft 前崩溃，可以重新 generation；
但已预占的 model_calls / token reservation 不回退。一旦 Proposal 已 checkpoint，就不能重新
generation 替换该 Proposal。

---

## 6. Contracts

所有 Contracts 继续继承 ContractModel：

~~~text
extra = forbid
frozen = true（ExecutionState / Usage 除外）
JSON serializable
bounded collections
stable enum / error code
~~~

### 6.1 Routing proposal

建议使用内部模型 DTO，不替换最终 RouteDecision：

~~~python
class CapabilitySelectionDraft(ContractModel):
    kind: Literal["direct"]
    capability_id: NonEmptyString
    confidence: float | None
    reason_code: NonEmptyString

class PlanRouteDraft(ContractModel):
    kind: Literal["plan"]
    confidence: float | None
    reason_code: NonEmptyString

class ExploreRouteDraft(ContractModel):
    kind: Literal["explore"]
    confidence: float | None
    reason_code: NonEmptyString

class HybridRouteDraft(ContractModel):
    kind: Literal["hybrid"]
    confidence: float | None
    reason_code: NonEmptyString
~~~

AUTO 使用按当前 availability 构造的判别联合 Schema。

固定 FAST 只使用 CapabilitySelectionDraft Schema。

RouteDecisionMaterializer 必须重新校验：

~~~text
route kind is currently available
capability exists and is directly executable
capability belongs to Policy scope
explicit target is not changed
explorer profile is server configured
source is model
~~~

### 6.2 RoutingContext 语义修正

当前 requested_mode 实际承载 effective mode。

3C 建议拆分：

~~~text
raw requested mode
  → RequestOptions / PRE_ROUTE / Trace

effective mode
  → RoutingContext.effective_mode / deterministic dispatch

model prompt
  → neither
~~~

为兼容旧 Router：

- requested_mode 属性保留一个版本，并标为 deprecated effective-mode alias；
- 新代码只读取 effective_mode；
- 旧字段不得再进入 LLM prompt。

### 6.3 StructuredOutputSpec

建议：

~~~python
class StructuredOutputStrictness(StrEnum):
    BEST_EFFORT = "best_effort"
    REQUIRED = "required"

class UnsupportedStructuredOutputBehavior(StrEnum):
    FAIL = "fail"
    JSON_OBJECT = "json_object"

class StructuredOutputSpec(ContractModel):
    name: NonEmptyString
    schema: FrozenJsonMapping
    strictness: StructuredOutputStrictness
    on_unsupported: UnsupportedStructuredOutputBehavior
~~~

GenerateRequest 以 additive 字段携带它：

~~~python
class GenerateRequest(ContractModel):
    # 旧字段保持
    response_format: ModelResponseFormat = ModelResponseFormat.TEXT
    response_schema: FrozenJsonMapping | None = None

    # 3C 新信任边界
    structured_output: StructuredOutputSpec | None = None
~~~

兼容规则：

- `structured_output=None` 时完全保持 3A / 3B legacy 语义；
- `structured_output` 与 `response_schema` 互斥，且要求 JSON response format；
- LLMRouter-v2、LLMPlanner-v2 和 ExplorationEngine 只使用 `structured_output`；
- StructuredGenerationAdapter 不得把 REQUIRED 请求改写成 legacy `response_schema`。

约束：

- REQUIRED 必须 on_unsupported=FAIL；
- 禁止 remote ref；
- 限制 schema 深度、节点数、枚举大小与字符串长度；
- Gateway 计算 schema_hash，观察面不保存完整 schema；
- Provider adapter 可以把通用 schema 编译为厂商支持子集，但不能放宽语义；
- 无法无损转换时 Provider 不 eligible。

### 6.4 ModelProviderFeatures

~~~python
class ModelProviderFeatures(ContractModel):
    json_object: bool = False
    json_schema: bool = False
    json_schema_strict: bool = False
    refusal_signal: bool = False
    usage_tokens: bool = True
    normalized_cost: bool = False
    cost_rate: NormalizedCostRate | None = None
~~~

boolean feature 只表示大类能力，不足以证明具体 Schema 可无损编译。
ModelProvider SPI 因此增加具有安全默认值的 additive 能力接口：

~~~python
class ModelProvider(Capability):
    @property
    def features(self) -> ModelProviderFeatures:
        # legacy provider 的默认值为 strict 不可用
        return ModelProviderFeatures()

    def prepare_structured_output(
        self,
        spec: StructuredOutputSpec,
    ) -> PreparedStructuredOutput | None:
        # 无网络、确定性、无损编译；legacy 默认返回 None
        return None

    async def generate_prepared(
        self,
        request: GenerateRequest,
        prepared: PreparedStructuredOutput,
        context: InvocationContext,
    ) -> GenerateResult:
        # legacy default 抛 STRUCTURED_OUTPUT_UNSUPPORTED，不得调用 generate() 降级
        ...

    def bound_input_tokens(
        self,
        request: GenerateRequest,
        prepared: PreparedStructuredOutput,
    ) -> int | None:
        # 必须是包含 message/schema/protocol overhead 的 sound upper bound
        return None
~~~

`PreparedStructuredOutput` 是 Gateway 内部的一次性 provider-specific 请求配置，至少包含
provider_id、`schema_hash` 与 `semantics_preserved=true`；不进入模型 Prompt 或
Plan checkpoint。

Provider Registry 在注册时从受信任 Provider 实例快照 `features`，不从请求 metadata
或模型输出读取。ModelGateway 选择前对每个候选执行
`prepare_structured_output(spec)`；只有大类 feature 满足且具体 Schema 无损编译成功的
Provider 才 eligible。编译在任何 generation 之前完成，失败不允许退化。

Gateway 为每个 eligible Provider 保存独立的 prepared mapping；ProviderExecutionCoordinator
选中某候选时，传入该 Provider 自己的 PreparedStructuredOutput 并且只调用
`generate_prepared`。fallback 时换用新 Provider 的 prepared object，禁止跨 Provider
复用。prepared.provider_id / schema_hash 与 request structured_output 不匹配时，在网络
调用前 fail-closed。BEST_EFFORT legacy 路径仍可调用现有 `generate()`。

Exploration 的 hard budget 需要两阶段 Gateway，不能让 Explorer 先按自己猜测预留，
然后再让 Gateway 重新选 Provider。新增 provider-neutral 持久化 Contract：

~~~python
class ModelGenerationAttemptSlot(ContractModel):
    slot_id: NonEmptyString
    provider_id: NonEmptyString
    provider_registration_version: NonEmptyString
    provider_features_hash: NonEmptyString
    prepared_schema_hash: NonEmptyString
    provider_attempt: int
    input_token_upper_bound: int
    output_token_upper_bound: int
    token_upper_bound: int
    normalized_cost_upper_bound: NormalizedCost | None

class ModelGenerationReservation(ContractModel):
    generation_id: NonEmptyString
    request_fingerprint: NonEmptyString
    schema_hash: NonEmptyString
    registry_snapshot_hash: NonEmptyString
    slots: tuple[ModelGenerationAttemptSlot, ...]
    total_token_upper_bound: int
    total_cost_upper_bound: NormalizedCost | None
    reservation_hash: NonEmptyString

class ModelReservationReceipt(ContractModel):
    execution_ref: PlanNodeRef
    exploration_id: NonEmptyString
    generation_id: NonEmptyString
    reservation_hash: NonEmptyString
    committed_state_version: int
    scheduler_generation: int
    owner_epoch: int
~~~

Gateway 内部 `PreparedModelGeneration` 包含上述可持久化 reservation 与每个 slot
对应的 opaque PreparedStructuredOutput，但后者不序列化。API 冻结为：

~~~python
prepared = await model_gateway.prepare_generation(request, context, attempt_policy)
receipt = await exploration_checkpoint_sink.reserve_model_generation(
    prepared.reservation,
)
result = await model_gateway.execute_prepared(prepared, receipt, context)
~~~

`prepare_generation` 只做本地 Catalog/feature/schema/token/cost/attempt-slot 冻结，零网络调用。
`reserve_model_generation` 在同一 CAS 中增加 model_calls、写入
ExplorationModelAttemptState(RESERVED) 并返回绑定 generation_id / reservation_hash /
state_version / scheduler_generation 的 `ModelReservationReceipt`。`execute_prepared` 没有匹配
receipt 时禁止任何 outbound。

Gateway 只能按 reservation.slots 的顺序、Provider registration version 与次数执行；
不得因 Registry 变化动态加入新 Provider / retry。`execute_prepared` 在每个 slot 首次 outbound
前还必须通过 ExplorationCheckpointSink 将该 slot 从 PREPARED CAS 为 STARTED，并取得绑定
slot_id / owner epoch / scheduler generation 的短期执行票据；CAS 或票据校验失败时 outbound=0。
Provider 返回后，完整 accounting 与 slot terminal 状态也要先 CAS，之后才可尝试下一个已预留
slot。accounting 不完整时整个 generation 直接 ORPHANED，不允许 fallback。

这个 slot STARTED CAS 必须读取最新 record；如果 cancel、approval takeover、Patch handoff 或
新的 scheduler generation 已先成功，旧 receipt 即失效，零 outbound。fallback slot 也重复同一
流程，不能因 generation 最初已经 reserved 而跳过最新 fencing。

3C v1 明确不跨进程恢复 opaque `PreparedModelGeneration`，也不提供
`reprepare_generation`。进程恢复时，任何 RESERVED / RUNNING generation——无论某个 slot
看起来仍是 PREPARED 还是已经 STARTED——都整体标为 ORPHANED，永久消耗 reservation；若剩余
预算允许，Explorer 只能以新的 generation_id 重新执行 prepare → reserve → execute。这样即使
Provider 已经 outbound / 计费但 accounting checkpoint 前崩溃，也不会重放同一 slot。

同一进程内的 retry / fallback 只允许消费 reservation 中尚未开始的下一个 slot，并且前一个
slot 已有完整、持久化的 terminal accounting。Provider Registry、registration version、feature
hash、schema hash 或计费率在 reserve 后变化时，当前 generation ORPHANED，不能替换 Provider。

`bound_input_tokens` 必须给出包含完整 messages、schema 传输与 Provider 协议 overhead
的可证明上界。可以使用受信 tokenizer，或使用经证明的保守 UTF-8 byte 上界；
无法给出 sound finite bound 的 Provider 对 budgeted Exploration 不 eligible。Gateway 同时
验证每个 slot.input_token_upper_bound 不超过 Profile 的 per-call 上限。

每个 slot 的 normalized cost 上界必须在 prepare 阶段按同一 unit 直接冻结：

~~~text
slot.normalized_cost_upper_bound
  = slot.input_token_upper_bound  * max_input_token_cost_per_token
  + slot.output_token_upper_bound * max_output_token_cost_per_token
  + finite request surcharge upper bound

reservation.total_cost_upper_bound
  = sum(slot.normalized_cost_upper_bound for every permitted slot)
~~~

存在 cost_limit 时，任一乘数、附加费或 unit 无法给出有限可信上界，该 Provider 对本次
Exploration 不 eligible；禁止用 rate 对象本身求和或把未知费用当 0。

Provider-neutral usage 不能让 `harness-contracts` 反向依赖 `harness-model`。
3C 将 `ModelUsage`、`NormalizedCost`、`NormalizedCostRate` 与下列聚合计费 Contract
下沉到 `harness-contracts`；`harness-model` 保留旧 import path 的 re-export：

~~~python
class ModelAttemptAccounting(ContractModel):
    # 单个 Provider adapter 返给 Gateway；provider_id / ordinal 由 Gateway 注入
    usage: ModelUsage | None
    normalized_cost: NormalizedCost | None
    complete: bool

class ModelProviderAttemptUsage(ContractModel):
    provider_id: NonEmptyString
    ordinal: int
    usage: ModelUsage | None
    normalized_cost: NormalizedCost | None
    complete: bool

class ModelGenerationAccounting(ContractModel):
    attempts: tuple[ModelProviderAttemptUsage, ...]
    aggregate_usage: ModelUsage
    aggregate_cost: NormalizedCost | None
    complete: bool

class GenerateResult(ContractModel):
    # 现有字段保留
    usage: ModelUsage | None
    # raw Provider -> Gateway；FAILED 也可携带，不算 successful output field
    attempt_accounting: ModelAttemptAccounting | None = None
    # Gateway -> caller 的聚合字段
    accounting: ModelGenerationAccounting | None = None
~~~

现有校验“FAILED 不能携带 success `usage`”保持；失败 Provider 已消耗的
token/cost 使用独立 `attempt_accounting` 返回。SUCCESS 的 attempt_accounting.usage
必须与 legacy `usage` 相等。Provider adapter 不得构造 aggregate `accounting`；Gateway
不得向最终 caller 暴露未验证的 raw attempt accounting。

`cost_rate` 提供与 profile token ceiling 配合的单次最坏费用上界；
`GenerateResult.accounting` 则汇总所有 retry / fallback Provider attempt 的实际 token
和 cost。开启 token/cost budget 的 Exploration 要求 `accounting.complete=true`；任何一次
attempt 无法计量时 fail-closed，不得把它当成 0。

ModelGateway 复用 ProviderExecutionCoordinator 时，必须在 `_invoke_selected` 收到每一个
GenerateResult 后，先交给 Harness-owned `ModelAccountingAccumulator`，再转换为
ResultEnvelope。成功和失败 attempt 都要记录，不得因 failure envelope 只保留
ErrorDetail 而丢弃 token/cost。timeout / crash 的 attempt 保留预留额并标记
`complete=false`。Gateway 同时对 attempt-started / completed 观察回调传递聚合事实。
它使用当前 selected provider_id 和单调 ordinal 包装 raw attempt_accounting，不信任
Provider metadata 中自报的 identity。

必测场景：第一个 Model Provider 已消耗 token 后失败，fallback Provider 成功；
ModelGenerationAccounting 的 total_tokens / cost 必须同时包含两个 attempt。

Provider feature 是模型协议能力，不是业务 Capability 权限。

### 6.5 PlannerOutputNormalizer / PlanTemplate / PlanMaterializer

PlanTemplate 不包含：

~~~text
plan_id
revision
request_id metadata
execution timestamps
provider identity
~~~

PlanMaterializer 输入：

~~~text
PlanTemplate / PlanDraft
InvocationContext
trusted planner / profile metadata
PlanIdentityFactory
Policy-clamped constraints
~~~

输出：

~~~text
ExecutionPlan(
  fresh plan_id,
  revision = 1
)
~~~

唯一 ownership：

~~~text
Planner.plan() -> ExecutionPlan candidate
        ↓
PlannerOutputNormalizer（丢弃 candidate plan_id / revision / runtime metadata）
        ↓
PlanTemplate
        ↓
RequestCoordinator-owned PlanMaterializer（恰好一次）
        ↓
executable ExecutionPlan
~~~

`PlanMaterializer` 同时接受原生 PlanTemplate / PlanDraft 以及经 Normalizer 转换的
legacy ExecutionPlan candidate。任何 Planner 输出中的 plan_id 都不能流入 StateStore。
内置 Planner 不得预先调用最终 Materializer，避免双重换 ID。

`execute_plan()` 不经过 Normalizer / Materializer；它仍然执行输入的具体 plan_id，
并依靠 create conflict 拒绝重复执行实例。

StaticPlanner 兼容策略：

1. 推荐 route value 改为 PlanTemplate 或 factory；
2. 旧 ExecutionPlan literal 被 Normalizer 视为模板内容，plan_id/revision 不复用；
3. 若调用方需要执行特定 plan_id，应使用 execute_plan；
4. 增加内置 StaticPlanner 和恶意/错误自定义 Planner 重复返回同一 ID 的阻断测试。

### 6.6 ExplorationProfile

ExplorationProfile 由 Composition Root 配置，不由模型选择：

~~~python
class PatchExecutionLimits(ContractModel):
    max_nodes: int
    max_edges: int
    max_provider_attempts_per_node: int

class ExplorationProfile(ContractModel):
    profile_id: NonEmptyString
    model_capability_id: NonEmptyString
    allowed_capability_ids: frozenset[NonEmptyString]
    default_budget: ExplorationBudgetTemplate
    allow_write: bool = False
    allow_external_egress: bool = False
    allow_plan_patch: bool = False
    patch_limits: PatchExecutionLimits | None = None
    max_input_tokens_per_model_call: int
    max_output_tokens_per_model_call: int
    prompt_version: NonEmptyString

class ExplorationPermissions(ContractModel):
    allow_write: bool = False
    allow_external_egress: bool = False
    allow_patch: bool = False
~~~

安全要求：

- allowed_capability_ids 必须显式非空；
- 不允许“缺省等于全部 Catalog”；
- MODEL 类型 Capability 不能进入 Action scope；
- profile 只含 capability IDs，不含 Provider / Plugin IDs；
- Policy 只能进一步收紧 profile。
- allow_plan_patch=false 时 patch_limits 必须为 None；true 时必须是有限正整数上限。

`ExplorationProfile` 是可复用的 composition-time 定义，因此它不得携带绝对
`deadline_at`。`ExplorationBudgetTemplate` 与 ExplorationBudget 维度相同，但使用
`max_duration_ms` 表示相对时长。ExplorationPlanFactory / PlanMaterializer 在 fresh execution
时取 `started_at + max_duration_ms`、Request deadline 与 Plan/Node deadline 的最早值，物化
为持久化 `ExplorationBudget.deadline_at`。

### 6.7 ExplorationBudget / Usage

~~~python
class NormalizedCost(ContractModel):
    unit: NonEmptyString
    amount: float  # >= 0

class NormalizedCostRate(ContractModel):
    unit: NonEmptyString
    max_input_token_cost: float  # >= 0
    max_output_token_cost: float  # >= 0

class ExplorationBudget(ContractModel):
    max_steps: int
    max_model_calls: int
    max_action_calls: int
    max_total_tokens: int | None
    cost_limit: NormalizedCost | None
    deadline_at: datetime | None
    max_repeated_actions: int
    max_patch_count: int
    max_exploration_depth: int
    max_observations: int

class ExplorationBudgetTemplate(ContractModel):
    max_steps: int
    max_model_calls: int
    max_action_calls: int
    max_total_tokens: int | None
    cost_limit: NormalizedCost | None
    max_duration_ms: int
    max_repeated_actions: int
    max_patch_count: int
    max_exploration_depth: int
    max_observations: int

class ExplorationUsage(MutableContractModel):
    steps: int
    model_calls: int
    action_calls: int
    total_tokens: int
    normalized_cost: NormalizedCost | None
    patch_count: int
~~~

为了让“Policy 只能收紧”可被实现和测试，新增可选维度的类型化约束：

~~~python
class ExplorationBudgetCeiling(ContractModel):
    max_steps: int | None = None
    max_model_calls: int | None = None
    max_action_calls: int | None = None
    max_total_tokens: int | None = None
    cost_limit: NormalizedCost | None = None
    deadline_at: datetime | None = None
    max_repeated_actions: int | None = None
    max_patch_count: int | None = None
    max_exploration_depth: int | None = None
    max_observations: int | None = None

class ExplorationPolicyConstraints(ContractModel):
    allowed_capability_ids: frozenset[NonEmptyString] | None = None
    budget_ceiling: ExplorationBudgetCeiling | None = None
    allow_write: bool | None = None
    allow_external_egress: bool | None = None
    allow_plan_patch: bool | None = None
~~~

`RoutePolicyConstraints` 与 `PlanningConstraints` 均增加可选
`exploration: ExplorationPolicyConstraints | None`。PRE_ROUTE 的原始 mapping 必须先解析
为该 Contract，禁止未知字段；RequestCoordinator 再把已收紧结果复制到
PlanningConstraints。`ExplorationPolicyConstraintReducer` 按下列规则合并多个
PRE_ROUTE Policy：

~~~text
allowed_capability_ids -> intersection
numeric / token / cost ceilings -> minimum（cost unit 必须相同）
deadline_at -> earliest
allow_* -> logical AND；None 表示不附加约束
empty scope / incompatible cost unit -> fail closed
~~~

standalone EXPLORE 在 ExplorationPlanFactory 中使用 PRE_ROUTE 结果；HYBRID 在
PlanMaterializer 中合并 PRE_ROUTE / PlanningConstraints 和 node requested subset。合并后的最终
scope / budget 写入 Harness-owned Plan / Exploration state，resume 不从可伪造 metadata 重建。

PRE_PLAN 仍然在最终 ExecutionPlan 上做 Policy 门禁，但
3C v1 不允许它用 opaque mapping 回写或就地收紧 Plan。如果 PRE_PLAN 声明的
硬性约束与已物化 Plan 不符，直接 fail-closed；Policy 不能修改已经 hash /
validate 的 Plan。未来若需要 post-plan clamp，必须单独设计 identity-free PRE_PLAN_DRAFT
两阶段协议，不在 3C 隐式加入。

PRE_PLAN `REQUIRE_APPROVAL` 在 3C 继续保持 3B 语义：稳定失败
`HARNESS.POLICY.PLAN_APPROVAL_UNSUPPORTED`。不新增 Plan-level waiting 协议。Patch 所需的
人工审批必须在 PRE_PATCH 发生；修订 Plan 之后的 PRE_PLAN 只能 ALLOW 或 DENY，
若返回 REQUIRE_APPROVAL 则零 mutation 拒绝 Patch。

Budget 合并：

~~~text
Request deadline
∩ Plan / Node timeout
∩ ExplorationProfile default
∩ PRE_ROUTE / PlanningConstraints
~~~

所有维度只允许取更小上限。

PRE_PATCH 只在具体 Patch 已存在后约束 Patch 新增工作；它不参与初始 Exploration 或普通
Action 的 Budget 计算。Patch 新增节点的预算是持久化 Exploration 剩余额度与 PRE_PATCH
约束的进一步交集。

cost unit 由服务端配置的 Model accounting contract 冻结；不同 unit 不能直接相加或比较。
Provider 无法提供相同 unit 的最坏计费上界与实际 usage 时，带 cost_limit 的探索必须
fail-closed。

每次逻辑 ModelGateway generation 必须先由 Gateway 生成唯一的
`ModelGenerationReservation`。它冻结 eligible Provider 的 registration / feature / schema
hash、严格有序的 retry / fallback slots，以及每个 slot 的 token / cost 上界；Explorer
不得根据 Profile 自行重算一份平行的 attempt budget。

~~~text
reserved_tokens
  = reservation.total_token_upper_bound

reserved_cost
  = reservation.total_cost_upper_bound

where each slot.normalized_cost_upper_bound
  = input_token_upper_bound  * max_input_token_cost_per_token
  + output_token_upper_bound * max_output_token_cost_per_token
  + finite request surcharge upper bound
~~~

因此预留额覆盖同一 logical generation 可能发生的所有 retry / fallback，不是只
覆盖第一次 Provider call。所有 slot 的 cost 必须使用相同的 NormalizedCost unit，reservation
总额是 slot 上界值的和，不是 `NormalizedCostRate` 对象的和。调用方不得把
`GenerateRequest.max_output_tokens` 设置得更大；Gateway 不得执行不在 reservation 中的
Provider 或额外 retry。
任何数量 / 费率无法给出有限上界时，不允许进入网络调用。

### 6.8 ExplorationTurnDraft

模型每个 turn 只能返回一个判别分支：

~~~python
class CallCapabilityDraft(ContractModel):
    kind: Literal["call_capability"]
    capability_id: NonEmptyString
    input: RequestInput
    expected_observation_type: NonEmptyString | None
    reason_code: NonEmptyString
    decision_summary: NonEmptyString | None

class FinishDraft(ContractModel):
    kind: Literal["finish"]
    output: ResultOutput
    evidence_refs: tuple[NonEmptyString, ...]
    reason_code: NonEmptyString
    decision_summary: NonEmptyString | None

class PlanPatchDraft(ContractModel):
    kind: Literal["plan_patch"]
    add_nodes: tuple[PlanNodeDraft, ...]
    add_edges: tuple[PlanEdge, ...]
    output_updates: FrozenOutputMapping
    evidence_refs: tuple[NonEmptyString, ...]
    reason_code: NonEmptyString
~~~

模型禁止输出：

~~~text
exploration_id
action_id
patch_id
step
status
budget
usage
provider_id
plugin_id
planner_id
explorer_id
idempotency_key
approval
plan_id
base_revision
new_revision
~~~

### 6.9 PlanNodeDraft

3B 的 PlanDraft 当前复用最终 PlanNode，导致模型理论上可以填写 retry_policy 和
idempotency_key。

3C 建议增加模型专用 PlanNodeDraft：

~~~text
允许模型：
node_id（Plan 内局部引用）
kind
capability_id 或 exploration intent
input bindings
failure intent

禁止模型：
trusted idempotency key
provider retry safety
exploration profile identity
final timeout / budget ceiling
provider / plugin identity
~~~

PlanMaterializer 根据 Capability execution profile、Policy 与服务端默认值生成最终 PlanNode。

### 6.10 ActionProposal

~~~python
class ActionProposal(ContractModel):
    action_id: NonEmptyString
    exploration_id: NonEmptyString
    step: int
    capability_id: NonEmptyString
    input: RequestInput
    idempotency_key: NonEmptyString | None
    proposal_hash: NonEmptyString
    catalog_snapshot_hash: NonEmptyString
    scope_hash: NonEmptyString
    reason_code: NonEmptyString
~~~

其中 action_id、idempotency_key、hash 均由 Harness 生成。

为避免 proposal_hash 与 idempotency_key 循环依赖，materialization 顺序冻结为：

1. 对 identity-free Action Draft 与受信任 `plan_id/node_id/exploration_id/step/
   catalog_snapshot_hash/scope_hash` 做 canonical JSON；
2. 计算 proposal_hash，hash payload 显式排除 `proposal_hash`、`action_id` 和
   `idempotency_key`；
3. 生成 fresh action_id；
4. 使用下列完整 execution identity 派生幂等键；
5. 构造最终 ActionProposal 并验证 hash / key 可重算。

幂等键稳定派生自：

~~~text
plan_id + node_id + exploration_id + action_id + proposal_hash
~~~

ActionValidator 检查：

~~~text
capability in explicit scope
capability type is AGENT or TOOL
input matches CapabilityDescriptor.input_schema
input depth / size / values bounded
side-effect / egress allowed
budget remaining
deadline remaining
repeated fingerprint threshold
no recursive exploration
no reserved control fields
~~~

### 6.11 ActionExecutionState

~~~python
class ProviderSelectionIntent(ContractModel):
    provider_id: NonEmptyString
    selection_key: NonEmptyString
    equivalence_group: NonEmptyString | None
    proposal_hash: NonEmptyString
    policy_decision_hash: NonEmptyString | None
    status: Literal["selected", "waiting_approval", "authorized"]

class ActionExecutionState(MutableContractModel):
    action_id: NonEmptyString
    status: ActionExecutionStatus
    proposal: ActionProposal
    selection_intent: ProviderSelectionIntent | None
    provider_history: list[ProviderAttempt]
    selected_provider_id: NonEmptyString | None
    provider_attempt: int
    provider_retry_attempt: int
    provider_selection_key: NonEmptyString | None
    provider_equivalence_group: NonEmptyString | None
    provider_last_result: ResultEnvelope | None
    result: ResultEnvelope | None
    continuation: Continuation | None
    approval: BoundApprovalState | None
    observation_id: NonEmptyString | None
    started_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None
~~~

Provider resume 字段应与 NodeExecutionState 采用相同语义，避免 Explore 建立另一套
WRITE safety。

`ProviderSelectionIntent` 只表示“已选定供 Policy 评估的 Provider”，不是
ProviderAttempt，不得发布 `attempt_started`。只有 PRE_EXECUTE ALLOW 后持久化的
attempt intent 才进入 provider_history。

### 6.12 Observation

~~~python
class Observation(ContractModel):
    observation_id: NonEmptyString
    source_ref: ExplorationActionRef | PlanPatchRef
    result_status: ResultStatus
    output_type: NonEmptyString | None
    bounded_summary: FrozenJsonValue
    evidence_refs: tuple[NonEmptyString, ...]
    result_hash: NonEmptyString
    error_code: NonEmptyString | None
~~~

StateStore 保存恢复所需的 ResultEnvelope；模型只看到 Observation 的有界投影。

evidence_refs 必须引用已有 Observation / 受信任 object reference，模型不能凭空发明。

### 6.13 ExplorationState

~~~python
class ExplorationState(MutableContractModel):
    exploration_id: NonEmptyString
    plan_id: NonEmptyString
    node_id: NonEmptyString
    created_plan_revision: int
    profile_id: NonEmptyString
    status: ExplorationStatus
    state_version: int
    budget: ExplorationBudget
    usage: ExplorationUsage
    permissions: ExplorationPermissions
    patch_limits: PatchExecutionLimits | None
    allowed_capability_ids: frozenset[NonEmptyString]
    scope_hash: NonEmptyString
    model_attempts: list[ExplorationModelAttemptState]
    actions: list[ActionExecutionState]
    observations: list[Observation]
    patches: list[PlanPatchExecutionState]
    pending_action_id: NonEmptyString | None
    pending_patch_id: NonEmptyString | None
    final_result: ResultEnvelope | None
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None
~~~

PlanExecutionState 新增：

~~~text
explorations: dict[node_id, ExplorationState]
revision_history: list[PlanRevisionAudit]
scheduler_generation: int
claimed_operation: ExternalOperationClaim | None
external_operation_history: list[ExternalOperationRecord]
patch_node_origins: dict[NonEmptyString, PatchNodeOrigin]
applied_transitions: list[AppliedExecutionTransition]
~~~

~~~python
class AppliedExecutionTransition(ContractModel):
    transition_id: NonEmptyString
    fact_hash: NonEmptyString
    committed_state_version: int
    committed_at: datetime

class ExternalOperationOutcome(ContractModel):
    status: Literal["completed", "rejected", "failed", "abandoned"]
    completed_state_version: int
    result_hash: NonEmptyString | None
    error_code: NonEmptyString | None

class ExternalOperationClaim(ContractModel):
    operation_id: NonEmptyString
    kind: Literal["resume", "approval", "async_completion", "cancel", "patch_handoff"]
    execution_ref: ExecutionUnitRef | None
    operation_input: ExternalOperationInput
    operation_input_hash: NonEmptyString
    phase: Literal["claimed", "running", "handoff_committed"]
    owner_id: NonEmptyString
    owner_epoch: int
    lease_expires_at: datetime
    claimed_scheduler_generation: int
    claimed_from_state_version: int
    claimed_at: datetime

class ExternalOperationRecord(ContractModel):
    claim: ExternalOperationClaim
    outcome: ExternalOperationOutcome
    completed_at: datetime
~~~

`ExternalOperationInput` 是按 kind 判别的受限 union：resume 保存目标 ref；approval 保存完整
Grant；async_completion 保存 execution_ref / expected_job_ref / terminal ResultEnvelope；cancel 保存
bounded reason；patch_handoff 保存 exact PlanPatchRef。它不得退化成任意 metadata map。approval Grant
或 async terminal payload 必须与 claim 在首次 CAS 中一起持久化，不能先 claim 再依赖进程内参数。

fresh create 时 scheduler_generation=1。每次 resume、Patch accept/reject handoff 或外部 operation
成功领取 WAITING record 时，必须在同一 CAS 中设置 claim、持久化 exact input，并使 generation
精确 +1。
每个 Scheduler / node / model / Provider callback 捕获启动时 generation，checkpoint 同时比对
plan_revision + scheduler_generation + state_version。旧 generation 失败后不得 reload
并自动 rebase/retry。`claimed_operation` 在该 operation 终结或完成 scheduler handoff
时，必须在同一 CAS 中连同 outcome 移入 append-only `external_operation_history` 后才能清除。

同一 operation_id + input hash 的 duplicate 返回 persisted outcome 或附着当前 claim；同一
operation_id + 不同 input hash 稳定失败。任何 worker 都不能覆盖活跃 claim。owner lease 到期后，
新 worker 只能以 CAS 增加 owner_epoch 与 scheduler_generation 接管，并继续 persisted input / phase；
旧 owner 所有 callback 因 fencing 失败。claim CAS 后、handoff 前崩溃不会永久卡住 record。

takeover 不等于允许重放不确定 outbound。每次模型 / Provider 出站仍必须有自己持久化的
attempt phase 与 idempotency/fencing：模型 STARTED crash 按 ORPHANED；WRITE 使用 3A 的
idempotency / ambiguity 规则；无法证明安全的 Provider STARTED 状态 fail-closed。只有明确未开始
或可安全恢复的 durable phase 才能继续。lease 不能被用来把“可能已经执行”解释成“尚未执行”。

旧 checkpoint 缺少这些字段时默认空集合，必须继续可恢复。
对旧且不含 EXPLORATION node 的 checkpoint，scheduler_generation 安全迁移为 1、
claimed_operation=None、external_operation_history=[]；旧 EXPLORATION record 继续按
RESUME_UNSAFE 处理。

PlanExecutionRecord 一致性校验必须确认：

~~~text
ExplorationState.plan_id == record.plan_id
ExplorationState.node_id 指向 EXPLORATION node
ExplorationState.exploration_id == ExplorationNodeSpec.exploration_id
ExplorationState.created_plan_revision <= record.plan.revision
ExplorationState.node_id 在 created revision 与当前 revision 中均指向同一 EXPLORATION node identity
~~~

`created_plan_revision` 是历史创建事实，Patch 后不改写。不能强制所有历史
ExplorationState 的 revision 与当前 Plan 相等，否则会破坏 append-only 审计语义。

`ExplorationRecoveryValidator` 在任何 resume / approval / async completion / cancel / patch CAS
之前验证完整 nested state：

~~~text
explorations map key == ExplorationState.node_id
exploration/action/patch/observation IDs 在对应边界唯一
pending_action_id 精确指向一个非终态 Action，或唯一合法的
  terminal-result-without-observation 过渡 Action；其他情况必须为 None
pending_patch_id 精确指向一个非终态 Patch，或同时为 None
Action proposal/ref/hash/result/continuation/job/provider selection/attempt facts 相互一致
Observation.source_ref 只指向已存在的 Action/Patch，result_hash 可重算
outer WAITING continuation == child WAITING ref == Plan pending index
Approval request/grant binding == current Proposal / Provider selection / Policy hash
BoundApprovalState status / Grant consumption / plan-level pending index 一致
claimed_operation 与 history operation_id 唯一，input hash / owner epoch / generation 可验证
applied transition_id 唯一、fact hash 稳定、committed_state_version 严格递增且不超过 record version
Model reservation/hash/total bounds/slot execution state 一致；非终态 restart 只能 ORPHANED
Patch-added node 与 ACCEPTED Patch / origin / ledger / deadline 一一对应
terminal Exploration / outer node / Plan status 与 ResultEnvelope / error / completed_at 一致
revision_history 从 1 开始、base/new 连续、hash 与当前 Plan 可重算
usage / attempt reservations / state_version 单调不回退
~~~

ResumeCoordinator 新增显式 `PlanNodeKind.EXPLORATION` 分支，不得把 RUNNING
EXPLORATION node 传入需要 Capability lookup 的旧 `_replay_safe` 逻辑。恢复顺序为
先验证 record，再依状态恢复同一 Proposal / Provider attempt / WAITING ref；任何损坏
checkpoint 以 `HARNESS.EXPLORATION.RESUME_UNSAFE` fail-closed，不得自我修复或重新询问模型。

### 6.14 ExecutionUnitRef

~~~python
class PlanNodeRef(ContractModel):
    kind: Literal["plan_node"] = "plan_node"
    plan_id: NonEmptyString
    node_id: NonEmptyString

class ExplorationActionRef(ContractModel):
    kind: Literal["exploration_action"] = "exploration_action"
    plan_id: NonEmptyString
    node_id: NonEmptyString
    exploration_id: NonEmptyString
    action_id: NonEmptyString

class PlanPatchRef(ContractModel):
    kind: Literal["plan_patch"] = "plan_patch"
    plan_id: NonEmptyString
    node_id: NonEmptyString
    exploration_id: NonEmptyString
    patch_id: NonEmptyString

ExecutionUnitRef = Annotated[
    PlanNodeRef | ExplorationActionRef | PlanPatchRef,
    Field(discriminator="kind"),
]
~~~

wire discriminator 固定为上述三个小写值，不根据字段存在性猜测 variant。
Continuation / Approval 的 legacy plan_id/node_id 与 execution_ref.plan_id/node_id 同时存在时
必须字节级相等；不匹配的 wire payload 在 Contract validation 阶段即被拒绝。

Approval 兼容扩展：

~~~python
class ApprovalRequest(ContractModel):
    # 旧 plan_id / node_id 保留
    execution_ref: ExecutionUnitRef | None = None
    proposal_hash: NonEmptyString | None = None
    provider_id: NonEmptyString | None = None
    policy_decision_hash: NonEmptyString | None = None

class ApprovalGrant(ContractModel):
    # 旧 plan_id / node_id 保留
    execution_ref: ExecutionUnitRef | None = None
    proposal_hash: NonEmptyString | None = None
    provider_id: NonEmptyString | None = None
    policy_decision_hash: NonEmptyString | None = None

class BoundApprovalState(MutableContractModel):
    request: ApprovalRequest
    decision: ApprovalDecision | None
    grant: ApprovalGrant | None
    status: Literal["waiting", "granted", "consumed", "rejected", "cancelled"]
    decision_input_hash: NonEmptyString | None
    consumed_by: ExecutionUnitRef | None
    resolved_at: datetime | None
    consumed_at: datetime | None
~~~

一致性：

- exploration ref 的 plan_id / node_id 必须定位到当前 EXPLORATION node；
- action_id 必须是当前唯一 pending action；
- patch_id 必须是当前唯一 pending patch；
- ApprovalGrant 必须匹配 proposal_hash；
- 外部输入不能用 ref 切换到其他 tenant / request / plan。

Request → Decision → Grant 的绑定规则：

1. Action / Patch ApprovalRequest 必须持久化 exact execution_ref 和 proposal_hash；
2. ApprovalDecision 只能按 approval_id 定位当前 persisted Request，不信任 decision.metadata
   中的 ref / hash；
3. resolver 从 persisted Request 原样复制 execution_ref / proposal_hash / provider_id /
   policy_decision_hash 到 Grant；
4. CapabilityInvoker / PRE_EXECUTE 或 PRE_PATCH 在执行前同时比对 approval_id、
   plan/node、execution_ref、proposal_hash、provider/policy binding 以及当前 persisted
   Proposal；
5. stale / duplicate / mismatched Grant fail-closed，不得降级到仅 plan_id/node_id 匹配。

ActionExecutionState 与 PlanPatchExecutionState 都直接保存 typed `BoundApprovalState`；不得继续
使用 3B legacy `state.metadata.approval_grants` 作为 Exploration 的授权来源。
`resolve_approval` 的首次 CAS 必须原子完成：验证 persisted Request、从 Plan-level pending index
移除 matching request、持久化 exact Decision / Grant 与 input hash、设置 status=granted 或
rejected、创建 ExternalOperationClaim，并使 scheduler_generation +1。CAS 后、PRE_EXECUTE /
PRE_PATCH 前崩溃时，恢复只使用这份 persisted Grant，不重复审批，也不从 API 参数重建。

Grant 只有在同一 execution_ref / proposal / provider / policy binding 上完成实际授权时，才在
checkpoint 中转为 consumed；Action 在 ProviderAttempt STARTED intent CAS 中消费，Patch 在 accept
或 governed reject CAS 中消费。terminal / cancelled 后 Grant 不可转移或复用。相同 approval
decision 的 duplicate 返回已持久化 outcome；不同 decision hash 稳定失败。

Action 的 PRE_EXECUTE Approval 必须同时绑定 selected provider 与导致审批的
policy decision hash；Patch Approval 没有 Provider，`provider_id=None`，但仍必须绑定
PRE_PATCH decision hash。批准后不允许用同一 Grant 换 Provider 或转移到重新生成的
Proposal。

policy_decision_hash 在 REQUIRE_APPROVAL 决策物化 ApprovalRequest 之前计算：

~~~text
sha256(canonical_json(
  policy phase,
  policy stable name + version,
  effect = require_approval,
  typed constraints,
  execution_ref,
  proposal_hash,
  selected provider_id or null,
  invocation fingerprint
))
~~~

hash payload 排除 timestamp、自由文本 reason 和 metadata。resolver 只复制 persisted hash。
批准后 PRE_EXECUTE / PRE_PATCH 使用 Grant 重新评估当前 Policy；若新结果 DENY 或
REQUIRE_APPROVAL，新结果优先。若新结果 ALLOW，只验证 Grant 仍绑定原始
REQUIRE_APPROVAL 事实与当前 Proposal，不要求新 ALLOW decision hash 等于旧
REQUIRE_APPROVAL hash。

### 6.15 ExplorationNodeSpec

~~~python
class ExplorationNodeSpec(ContractModel):
    profile_id: NonEmptyString
    exploration_id: NonEmptyString
    requested_capability_ids: frozenset[NonEmptyString]
    budget: ExplorationBudget
    allow_patch: bool
    patch_limits: PatchExecutionLimits | None
~~~

PlanNode 规则：

~~~text
kind = EXPLORATION
capability = None
exploration spec required
retry_policy.max_attempts = 1
idempotency_key = None
input_mapping allowed
failure_policy allowed
~~~

exploration_id / profile_id 由 Harness materialize。

PLAN mode 默认不允许 EXPLORATION node；只有 HYBRID 或 Harness-owned standalone
EXPLORE wrapper 可启用。

### 6.16 PlanPatchProposal

~~~python
class PlanPatchProposal(ContractModel):
    patch_id: NonEmptyString
    plan_id: NonEmptyString
    base_revision: int
    source_exploration_id: NonEmptyString
    add_nodes: tuple[PlanNode, ...]
    add_edges: tuple[PlanEdge, ...]
    output_updates: FrozenOutputMapping
    proposal_hash: NonEmptyString
    reason_code: NonEmptyString
    evidence_refs: tuple[NonEmptyString, ...]
~~~

模型 Draft 到正式 Proposal 的 materialization 必须：

~~~text
inject plan / revision / patch identity
clamp node policy / retry / timeout / budget
resolve exploration profile
reject capabilities outside scope
~~~

Patch proposal_hash 的 canonical payload 包含 plan_id、base_revision、
source_exploration_id、物化后的 nodes/edges/outputs 与 evidence refs，显式排除
`patch_id` 和 `proposal_hash` 本身。顺序为先规范化 candidate 内容与计算 hash，
再生成 patch_id 和最终 Proposal。Approval / Resume 重算必须得到同一 hash。

### 6.17 PlanRevisionAudit

~~~python
class PlanRevisionAudit(ContractModel):
    patch_id: NonEmptyString
    base_revision: int
    new_revision: int
    proposal_hash: NonEmptyString
    source_exploration_id: NonEmptyString
    applied_at: datetime
    policy: NonEmptyString
~~~

RevisionAudit 是 execution truth，必须进入 PlanExecutionRecord。

Event 不是唯一审计来源。

### 6.18 PlanPatchExecutionState

~~~python
class PlanPatchBudgetReservation(ContractModel):
    added_node_count: int
    reserved_action_calls: int
    max_provider_attempts_per_node: int
    provider_attempt_limits_by_node: dict[NonEmptyString, int]
    reserved_provider_attempts: int
    deadline_at: datetime

class PlanPatchExecutionLedger(MutableContractModel):
    consumed_provider_attempts: int = 0
    consumed_provider_attempts_by_node: dict[NonEmptyString, int]

class PatchNodeOrigin(ContractModel):
    node_id: NonEmptyString
    patch_id: NonEmptyString
    proposal_hash: NonEmptyString
    accepted_node_kind: PlanNodeKind
    capability_id: NonEmptyString | None
    capability_security_descriptor_hash: NonEmptyString | None
    source_scope_hash: NonEmptyString
    deadline_at: datetime

class PlanPatchExecutionState(MutableContractModel):
    patch_id: NonEmptyString
    status: PlanPatchExecutionStatus
    proposal: PlanPatchProposal
    proposal_hash: NonEmptyString
    base_revision: int
    proposal_state_version: int
    cas_expected_state_version: int | None
    budget_reservation: PlanPatchBudgetReservation
    execution_ledger: PlanPatchExecutionLedger
    approval: BoundApprovalState | None
    continuation: Continuation | None
    error: ErrorDetail | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
~~~

它必须支持：

~~~text
PROPOSED
WAITING_APPROVAL
ACCEPTED
REJECTED
CONFLICTED
CANCELLED
~~~

Approval / Restart 必须从同一个 Proposal 和 proposal hash 继续，不能重新让模型生成 Patch。

`proposal_state_version` 是 Proposal 首次 checkpoint 后的审计事实，不是最终 CAS
的 expected version。Scheduler quiesce、approval waiting 或其他 in-flight node 在此之后
可能产生合法 checkpoint。`cas_expected_state_version` 只能在 quiesce 完成后、
PlanCheckpointCoordinator 锁内 reload latest record 时确定，并作为同一次
compare-and-save candidate 的字段写入 accepted record。

`provider_attempt_limits_by_node` 的 key 必须精确等于新增 CAPABILITY node 集合，总和等于
reserved_provider_attempts；ledger 的 per-node 计数总和必须等于 consumed_provider_attempts。
Patch accept CAS 同时把每个新增节点的 `PatchNodeOrigin` 写入
PlanExecutionState.patch_node_origins。普通 Planner 产生的节点不得拥有该 origin；任何 Patch-added
节点缺失、重复或指向非 ACCEPTED patch 的 origin 都是不可恢复的 checkpoint corruption。

Scheduler admission、Invoker dispatch 与每个 retry / fallback outbound 都从 origin 解析同一个
accepted Patch reservation / ledger，检查 absolute deadline，并重算当前 Catalog descriptor 的
security hash。只有 descriptor 仍是 accept 时允许的 capability type、READ/无外部 egress、仍属于
persisted source scope 且 hash 精确匹配时才可继续；变化时零 outbound fail-closed。每次 outbound
的 ledger 递增使用 PlanCheckpointCoordinator CAS，不能只记进程内 counter。

### 6.19 ExplorationModelAttemptState

~~~python
class ModelAttemptSlotExecutionState(MutableContractModel):
    slot_id: NonEmptyString
    status: Literal["prepared", "started", "completed", "failed", "orphaned"]
    owner_epoch: int | None
    accounting: ModelProviderAttemptUsage | None
    started_at: datetime | None
    completed_at: datetime | None

class ExplorationModelAttemptState(MutableContractModel):
    generation_id: NonEmptyString
    turn: int
    kind: Literal["initial", "repair"]
    status: Literal["reserved", "running", "completed", "failed", "orphaned"]
    reservation: ModelGenerationReservation
    reservation_hash: NonEmptyString
    registry_snapshot_hash: NonEmptyString
    reserved_tokens: int
    reserved_cost: NormalizedCost | None
    receipt_state_version: int
    receipt_scheduler_generation: int
    slots: list[ModelAttemptSlotExecutionState]
    accounting: ModelGenerationAccounting | None
    schema_hash: NonEmptyString
~~~

`reservation_hash`、registry/schema hash、reserved_tokens / reserved_cost 是便于索引与审计的
冗余字段，RecoveryValidator 必须验证它们分别等于 reservation 中的 canonical 值；不能成为第二
份预算来源。slots 与 reservation.slots 按 slot_id 一一对应，顺序和数量不可漂移。

逻辑 generation 的顺序严格是：

1. Gateway 本地 `prepare_generation` 生成 reservation，零网络；
2. BudgetGuard 用 reservation.total_* 检查剩余额度；
3. 同一 CAS 增加 model_calls、写入 generation=reserved 与全部 slot=prepared；
4. 返回 ModelReservationReceipt；
5. `execute_prepared` 在每个 Provider outbound 前再 CAS 对应 slot=started。

成功后按所有 Provider attempts 的聚合 accounting reconcile；crash 后未完成 reservation 标为 orphaned，
额度不返还。不能因为模型调用“没有业务副作用”而忽略真实 token / cost 副作用。

跨进程恢复不执行 persisted RESERVED / RUNNING generation。恢复事务把 generation 及所有未终态
slot 标为 ORPHANED；旧 owner 的回调因 scheduler_generation / owner_epoch / state_version 不匹配
而被拒绝。只有新的 generation_id 可以再次调用模型。这个保守规则也适用于“已预留但尚未观察到
slot STARTED”的 crash window，因为 opaque prepared object 与真实 outbound 状态都不在持久化边界。

ExplorationUsage 中的 token/cost 只记已知实际值，保持单调。BudgetGuard 的
实时占用额另按下式计算：

~~~text
effective token charge
  = ExplorationUsage.total_tokens
  + sum(reserved_tokens for RESERVED, RUNNING or ORPHANED generations)

effective cost charge
  = ExplorationUsage.normalized_cost
  + sum(reserved_cost for RESERVED, RUNNING or ORPHANED generations)
~~~

COMPLETED attempt 从 reservation 项移除，并把聚合实际值加入 Usage。
FAILED 只允许表示 `accounting.complete=true` 的已知失败（包括 refusal / truncated /
content filter / 后续 Draft validation failure）；它同样原子累加实际值并释放
reservation。无法取得完整 accounting 的 timeout / crash / failed generation 必须转为
ORPHANED，永久按预留额占用，不得使用 FAILED 让额度消失。若实际值超过
预留上界，说明 Provider accounting contract 违约，
Exploration fail-closed 且按更大值计费。

### 6.20 PlanExecutionProfile

PlanValidator 不能依赖可伪造 metadata 判断 EXPLORATION node 是否允许。

PlanExecutionRecord 新增 Harness-owned：

~~~python
class PlanExecutionProfile(ContractModel):
    origin: Literal["prebuilt", "planned", "explore_wrapper", "hybrid"]
    selected_mode: ExecutionMode
    allowed_node_kinds: frozenset[PlanNodeKind]
    allowed_exploration_profile_ids: frozenset[NonEmptyString]
    patch_enabled: bool
    checkpoint_cas_required: bool

class PlanExecutionRecord(ContractModel):
    # 其他现有字段省略；None 仅用于旧 wire 读取
    execution_profile: PlanExecutionProfile | None = None
~~~

首次执行时由 RequestCoordinator / ExecutionEngine 根据已验证 Route、配置与 Policy 创建；
resume、Patch 和再次 PlanValidator 使用持久化 profile。execute_plan 的输入仍不受信任，默认
prebuilt profile 只允许 CAPABILITY / APPROVAL；若请求执行 EXPLORATION node，必须经过显式
EXPLORE/HYBRID mode、配置和 Policy 验证后由 Harness 生成对应 profile。

任何包含 EXPLORATION node 的 Plan（standalone EXPLORE 或 HYBRID）都必须
`checkpoint_cas_required=true`，与 allow_patch 无关。原因是 approval / async completion /
cancel / resume 可以由多进程并发提交；仅靠本进程 lock 不能防止重复模型或
Action 执行。

旧 checkpoint 缺少 execution_profile 时，PlanRecordMigration 在任何执行前将 None
物化为：

~~~text
origin = prebuilt
selected_mode = PLAN
allowed_node_kinds = {CAPABILITY, APPROVAL}
allowed_exploration_profile_ids = empty
patch_enabled = false
checkpoint_cas_required = false
~~~

迁移器不得根据 metadata 或节点内容“猜测” HYBRID / EXPLORE 权限。Record
一致性验证还必须确认 selected_mode / origin / allowed_node_kinds / patch_enabled
与当前 Plan shape 一致。旧 record 若竟然包含 EXPLORATION node 却没有 profile，
直接 RESUME_UNSAFE；迁移完成后的 in-memory / newly-saved record 禁止 profile=None。

---

## 7. Exploration 执行算法

### 7.1 输入投影

每次模型 decision 输入：

~~~text
bounded goal
profile prompt version
filtered capability descriptors
remaining budget
bounded observations
prior decision validation codes（仅 repair）
dynamic strict output schema
~~~

不输入：

~~~text
Provider registrations
Plugin identity
Invocation baggage
Approval grants
full PlanExecutionState
raw prior prompts / outputs
credentials
hidden reasoning
~~~

### 7.2 主循环

~~~text
load / create ExplorationState
  ↓
check cancellation / deadline / budget
  ↓
build bounded decision input
  ↓
ModelGateway.prepare_generation（零网络，冻结 slots / bounds）
  ↓
BudgetGuard checks exact reservation.total_*
  ↓
CAS checkpoint generation=RESERVED / slots=PREPARED / model_calls+1
  ↓
ModelGateway.execute_prepared(receipt)
  ↓
before each Provider outbound: CAS slot=STARTED + fencing ticket
  ↓
persist complete per-slot / aggregate accounting
  ↓
validate ExplorationTurnDraft
  ├── CallCapabilityDraft
  │      ↓
  │   materialize ActionProposal
  │      ↓
  │   ActionValidator
  │      ↓
  │   checkpoint PROPOSED
  │      ↓
  │   ScopedActionExecutor
  │      ↓
  │   Result → Observation → checkpoint → next turn
  │
  ├── FinishDraft
  │      ↓
  │   evidence / output validation
  │      ↓
  │   terminal checkpoint
  │
  └── PlanPatchDraft
         ↓
      mode / profile allow?
         ↓
      materialize + checkpoint
         ↓
      PlanPatchCoordinator
~~~

`prepare_generation` 返回的 opaque prepared object 只在当前进程、当前 scheduler generation
存活；crash 后不得重建并继续同一个 reservation。Budget reservation CAS 失败、slot STARTED
CAS 失败或 receipt / owner fencing 不匹配时，模型 outbound 必须为 0。

### 7.3 Decision repair

模型 Draft 非法时允许有限 repair，但它仍属于模型 generation，不是 Action retry。

建议默认：

~~~text
max_decision_attempts_per_turn = 2
~~~

Repair 输入只包含：

~~~text
same bounded context
same dynamic schema
sanitized validation codes / locations
bounded previous JSON
~~~

每次 repair：

- 消耗 model_calls；
- 累计 token/cost；
- generation 前先持久化额度 reservation，crash 后不返还；
- 受 absolute deadline；
- 不创建 Action；
- 不触发 Capability；
- 达到上限返回 INVALID_DECISION / REPAIR_EXHAUSTED。

### 7.4 repeated action guard

指纹：

~~~text
sha256(capability_id + canonical_json(input))
~~~

如果没有新增 Observation evidence 而连续提出相同指纹：

- 第一次重复可作为结构化拒绝反馈；
- 达到 max_repeated_actions 后 fail-closed；
- 不把重复 Action 当成 Provider retry；
- 模型改变无关 reason_code 不改变指纹。

### 7.5 Final validation

FinishDraft 必须：

~~~text
output JSON bounded
output type allowed
evidence refs exist
no reserved metadata
no provider / plugin identity
no unsupported claim of execution
~~~

Budget exhausted 且已有 Observation 时，Harness 可以返回：

~~~text
ResultStatus.PARTIAL
output.type = "harness.exploration.partial"
output.data = bounded observation index / evidence refs
issue = BUDGET_EXHAUSTED
~~~

Harness 不应伪造业务结论。

---

## 8. Scope、Policy 与执行安全

### 8.1 Scope 计算

最终 scope：

~~~text
configured ExplorationProfile
∩ current Capability Catalog
∩ PRE_ROUTE allowed capabilities
∩ Plan / Node requested subset
~~~

任意一层为空：

~~~text
fail closed
~~~

Scope hash 进入 ExplorationState；Catalog snapshot hash 进入 Proposal。

Patch 新增节点 scope 单独计算：

~~~text
persisted Exploration scope
∩ current Capability Catalog
∩ PRE_PATCH allowed capabilities
~~~

PRE_PATCH 不能扩大 persisted scope。

Resume 时：

- 已 checkpoint Action 按原 snapshot / Provider resume 事实恢复；
- 新 turn 必须重新确认 Capability 仍存在且仍允许；
- Catalog 或 Policy 收紧可以拒绝后续动作；
- 不允许因 Catalog 扩大自动扩大持久化 scope。
- 最终 allow_write / allow_external_egress / allow_patch 作为 ExplorationPermissions 持久化；
- resume 只能用 persisted permissions 与当前 Profile/Policy 再做 logical AND，不得因
  配置变化把 false 恢复成 true。
- PatchExecutionLimits 同时写入 ExplorationNodeSpec 和 ExplorationState；resume /
  approval 只取 persisted limits 与当前配置的逐维 minimum，不得因配置放宽
  而增加 node/edge/provider-attempt 上限。

### 8.2 ScopedActionExecutor 顺序

~~~text
Action schema / input schema
  ↓
scope / side-effect / egress
  ↓
budget / deadline / recursion / repeated action
  ↓
checkpoint proposal
  ↓
CapabilityInvoker
  ↓
Provider resolution
  ↓
persist ProviderSelectionIntent（尚未开始 attempt）
  ↓
PRE_EXECUTE with selected Provider / matching Grant
  ├── REQUIRE_APPROVAL → checkpoint selection + approval binding → WAITING
  └── ALLOW
          ↓
persist ProviderAttempt STARTED intent
  ↓
Provider Retry / Fallback execution
~~~

3C 不新增绕过 CapabilityInvoker 的 PRE_ACTION 执行通道。

Proposal-level 的确定性 Guard 不替代 PRE_EXECUTE。Provider selection 不等于远程执行；
不得在 PRE_EXECUTE ALLOW 之前创建 FAILED / STARTED ProviderAttempt。这需要重构
3A 当前“attempt_started 包裹整个 invoke_provider”的边界，但不改变 3A 已冻结的
retry/fallback 安全语义。

### 8.3 WRITE Action

WRITE 必须满足 3A 已有规则：

~~~text
stable Harness-owned idempotency key
same Provider retry first
cross-Provider fallback only with same non-empty equivalence_group
checkpoint provider selection / attempt facts
ambiguous crash fail closed
~~~

Profile.allow_write=false 时，WRITE Proposal 在调用 Provider 前被拒绝。

每一次真实 Provider outbound call 的顺序必须是：

~~~text
select / resolve Provider
  ↓
persist ProviderSelectionIntent through PlanCheckpointCoordinator
  ↓
PRE_EXECUTE ALLOW / matching bound Grant
  ↓
persist ProviderAttempt STARTED intent through PlanCheckpointCoordinator
  ↓
outbound Provider call
  ↓
persist terminal / waiting result
~~~

只保存 ActionProposal 而未保存 selected Provider / attempt intent，不足以证明 WRITE crash
recovery 安全。Selection intent 与 ProviderAttempt 必须是两个不同的稳定边界。

### 8.4 Approval

PRE_EXECUTE REQUIRE_APPROVAL：

~~~text
ActionProposal already checkpointed
  ↓
ApprovalRequest(exploration action ref + proposal hash)
  ↓
outer Plan / Explore node WAITING
  ↓
resolve_approval(plan_id, decision)
  ↓
CAS: persist exact Decision / Grant in BoundApprovalState,
     remove matching pending index, claim new generation
  ↓
approved Grant bound to same action（crash-safe）
  ↓
pin same selected Provider / re-run PRE_EXECUTE
  ↓
persist ProviderAttempt intent / resume same Action
~~~

批准后不得重新询问模型生成另一个 Action，也不得重新自由 selection。
若原 selected Provider 已不存在、不再 eligible 或 policy hash 已不匹配，旧 Grant
fail-closed；必须产生新的 Proposal / Approval，不得将旧授权迁移到替代 Provider。
若在 Grant CAS 后、PRE_EXECUTE 或 ProviderAttempt STARTED checkpoint 前崩溃，resume 读取
同一个 BoundApprovalState 继续；不得重复审批，也不得回读 legacy metadata。

### 8.5 Async

Capability 返回 ACCEPTED：

~~~text
ActionState WAITING
Continuation carries exploration action ref
outer EXPLORATION node WAITING
whole PlanExecutionRecord checkpoint
~~~

兼容扩展后的入口：

~~~python
complete_async_node(
    plan_id,
    node_id,
    terminal_result,
    *,
    execution_ref=None,
    expected_job_ref=None,
)
~~~

处理顺序：

1. 加载 plan / node；
2. 如果是 CAPABILITY node，保持 Stage 2 逻辑；
3. 如果是 EXPLORATION node，强制要求 ExplorationActionRef；
4. ref、action_id、proposal hash 与 expected_job_ref 必须匹配持久化 pending Action；
5. 迟到、重复或指向旧 Action 的 callback fail-closed；
6. 将 terminal result 写入指定 ActionState；
7. 生成 Observation；
8. 恢复 Exploration loop；
9. 只有 Exploration terminal 后才完成外层节点。

### 8.6 Action failure 与 Approval rejection

3C v1 不让模型选择 action failure policy，固定映射为：

| 安全结果 | Action | Exploration | 是否可产生 Observation 后继续 |
|---|---|---|---:|
| 允许执行后的 SUCCESS / PARTIAL | SUCCEEDED | RUNNING | 是 |
| 允许执行后的业务 / Provider FAILED | FAILED | RUNNING | 是，仍受 action/repeat budget |
| ActionValidator / PRE_EXECUTE / Capability 返回 DENIED | DENIED | DENIED | 否，直接终止 |
| ApprovalDecision=REJECTED | DENIED | DENIED | 否，直接终止 |
| deadline / RESUME_UNSAFE | FAILED | FAILED | 否 |
| cancel | CANCELLED | CANCELLED | 否 |

政策拒绝和人工拒绝不作为可供模型“换一个 Action”的反馈，防止通过
反复提案绕过治理。仅已经通过 Policy 并发生了真实业务执行的失败才可
转换为有界 Observation。outer EXPLORATION node 再按它已验证的 node
failure_policy 对 Plan 传播终态。

### 8.7 Cancellation 与迟到输入失效

`cancel_plan(plan_id)` 不能只查当前进程的 `_active` signal。3C 扩展后的语义：

~~~text
active execution
  -> signal cancellation -> quiesce -> checkpoint terminal state

WAITING / restarted execution
  -> load persisted record -> per-plan lock/CAS -> checkpoint terminal state
~~~

取消 EXPLORATION node 时必须在同一 PlanExecutionRecord 中原子完成：

1. pending Action 若存在，转为 CANCELLED 并清除 pending_action_id；
2. pending Patch 若存在，转为 CANCELLED 并清除 pending_patch_id；
3. ExplorationState 与 outer NodeExecutionState 转为 CANCELLED；
4. PlanExecutionState 按 Stage 2 规则收敛为 CANCELLED；
5. pending Approval / Continuation / expected job ref 在该 record 中失效；
6. 所有包含 EXPLORATION node 的 record 取消都必须经 PlanCheckpointCoordinator
   和 versioned CAS。

取消之后到达的 ApprovalDecision、ApprovalGrant 或 async callback 必须因 terminal
status / ref / job mismatch fail-closed，不得恢复计划。对外部 job 发送取消只能是
best-effort 运输动作；持久化的 Harness 终态才是执行真相。

---

## 9. PlanPatch 运行语义

### 9.1 为什么第一版严格 append-only

运行中的 Plan 已经可能存在：

~~~text
completed results
running Provider calls
waiting approvals
async continuations
provider selection history
downstream readiness decisions
~~~

允许模型替换或删除既有节点会让这些事实失去含义。

因此 v1 只允许在现有 Plan 之外追加受验证的新工作，并受限更新最终 outputs。

v1 产品语义明确收窄为“追加新 tail”：

- 所有新 edge 的 to_node 必须是新增节点；
- 每个新增子图必须从 source EXPLORATION node 或另一个已存在的安全 predecessor 可达；
- Patch 可以把 Plan final outputs 改为引用新增 tail；
- Patch 不能把新增证据注入或增加为既有 downstream node 的依赖；
- 需要动态 tail 的 HYBRID Planner 必须把 Exploration node 设计为 barrier；
- 对尚未 READY 的既有节点增加 incoming edge / 替换 binding 属于后续 Patch v2。

因此 3C 不宣称可以任意把新节点“插入”已存在的 approval/report 链；总阶段示例实施时应按
“Explore barrier → new tail → revised outputs”解释。

PlanValidator 对 `allow_patch=true` 的 EXPLORATION node 施加强制的 execution-wide barrier
结构：所有可能在它之前执行的节点都必须是其祖先，所有尚未执行的既有节点都必须是其后代，
不存在可与它并行 READY / RUNNING / WAITING 的独立分支。Scheduler 也在进入该 node 前断言
其他既有节点均为 terminal、后继均未 admission。不是 barrier 的 Exploration 仍可运行，但其
profile 必须 `allow_patch=false`。这是 3C v1 能安全 quiesce 的前置条件，不是 Planner 建议。

### 9.2 Patch 应用

~~~text
ExplorationTurnDraft(kind=plan_patch)
  ↓
evidence refs validation
  ↓
Harness assigns patch identity / base revision
  ↓
checkpoint PlanPatchExecutionState(PROPOSED)
  ↓
ExplorationNodeExecutor returns PlanMutationSuspension
  ↓
Scheduler stops admitting new nodes
  ↓
wait all in-flight nodes to terminal / WAITING boundary
  ↓
return control to ExecutionEngine
  ↓
PlanCheckpointCoordinator acquires per-plan lock
  ↓
load latest record / verify scheduler generation
  ↓
base revision / persisted Proposal / proposal hash check
  ↓
cas_expected_state_version = latest.state_version（锁内）
  ↓
append-only structure / scope / budget / depth validation
  ↓
PRE_PATCH（approval resume 也从此完整重跑）
  ↓
materialize candidate revision + 1
  ↓
PlanValidator with persisted PlanExecutionProfile
  ↓
PRE_PLAN on candidate
  ↓
build migrated PlanExecutionState
  ↓
StateStore.compare_and_save(cas_expected_state_version)
  ↓
reload revision N + 1
  ↓
start a new Scheduler generation
~~~

PlanPatchCoordinator 不能在旧 Scheduler 的节点 callback 内直接替换 Plan 后继续运行。

EXPLORATION-containing Plan 的所有 checkpoint，不仅 Patch checkpoint，都必须经过同一个
PlanCheckpointCoordinator：

~~~text
per-plan lock
+ scheduler_generation
+ plan_revision
+ current state_version as CAS expected value
+ compare-and-save
~~~

旧 generation callback 在 revision N+1 生效后即使迟到，也会因 generation / revision /
state version 不匹配而 fail-closed，不能 unconditional save revision N 覆盖新记录。

resolve_approval、complete_async_node、cancel_plan 和 resume_plan 都先 load version V 并为该
external operation 生成唯一 operation_id，再使用 CAS 把 WAITING record 转为“已领取 /
已终结”状态。只有 CAS 成功者可创建新 Scheduler generation 或继续 Exploration
loop；并发 worker、duplicate callback 与迟到 Grant 必须在任何模型 / Provider 调用之前
失败。

Quiesce 要求：

- Patch Proposal 产生时没有 in-flight Action；
- Scheduler 停止启动新节点；
- 合法 v1 Plan 因 execution-wide barrier 不存在其他 RUNNING / WAITING node；
- runtime 仍有 bounded quiesce check，用于发现迁移、损坏 checkpoint 或旧 callback；
- check 超时不得启动新 scheduler / model turn，也不得把它当 recoverable Patch rejection；
- timeout 的 terminal CAS 使 scheduler_generation +1，Patch=REJECTED、source Exploration / outer
  node / Plan=FAILED，写入 matching `HARNESS.PLAN_PATCH.QUIESCE_TIMEOUT` Result / issue / timestamps，
  清除并失效所有 pending refs；已 STARTED 的 Provider attempt 标为 ambiguous/cancel-requested，
  迟到 callback 被 fencing 拒绝。

外部 Provider 的真实调用可能无法撤销，因此 QUIESCE_TIMEOUT 只保证不重放、不启动新工作和
execution truth 收敛，不虚假承诺远端副作用已经取消。新产生的 3C Plan 若触发此分支，视为
PlanValidator / Scheduler invariant violation。

CAS 成功前：

~~~text
Scheduler 不得根据 candidate Plan 启动新节点
~~~

CAS 失败：

~~~text
HARNESS.PLAN_PATCH.REVISION_CONFLICT
零 Plan mutation
零新增 Capability invocation
~~~

CAS 成功后必须先重新加载 persisted revision N+1，再创建新 Scheduler；内存 candidate 不能直接
作为执行真相。

Patch 在 suspension / quiesce 之后被拒绝时，也不得复用已停止的 Scheduler：

~~~text
recoverable INVALID / REVISION_CONFLICT
  -> lock + reload latest
  -> revalidate exact patch/ref/hash can still attach to source exploration
  -> CAS Patch=REJECTED/CONFLICTED + Observation + clear pending
  -> plan revision remains N; scheduler_generation + 1
  -> reload persisted revision N
  -> start new Scheduler generation and resume source Exploration

Policy DENY / human rejection / PRE_PLAN deny-or-approval-unsupported
  -> lock + reload latest
  -> CAS Patch=REJECTED + Exploration/outer node/Plan governed terminal state
  -> clear pending; scheduler_generation + 1
  -> no new model turn and no new Scheduler execution
~~~

记录 rejection 的 CAS 如果竞争失败，Coordinator 只能有界 reload 并重验同一
Proposal 的可附着性；禁止把 Patch application rebase 到新 revision。若 source/ref 已
被其他 terminal transition 消费，当前 operation fail-closed 且不覆盖 latest record。

### 9.3 PRE_PATCH

PolicyPhase 新增 PRE_PATCH。

PolicyContext 至少包含：

~~~text
InvocationContext
current Plan
PlanPatchProposal
source ExplorationActionRef / PlanPatchRef
bounded patch summary
~~~

效果：

- ALLOW：继续 materialize candidate、完整 PlanValidator、PRE_PLAN 与 CAS；
- DENY：Patch REJECTED，Exploration / outer node 以 DENIED 终止，不再返回模型换 Proposal；
- REQUIRE_APPROVAL：PlanPatchExecutionState 保存 ApprovalRequest / Continuation，外层 Plan
  进入 WAITING。

ApprovalDecision=REJECTED 与 Policy DENY 使用相同 governed terminal 语义。PRE_PLAN
DENY 或 REQUIRE_APPROVAL（本阶段不支持）也是 terminal rejection，零 Plan mutation。

Approval resume 必须从最新 record 重新执行完整链路：

~~~text
base revision / state version / proposal hash
  ↓
append-only / scope / budget / depth validation
  ↓
PRE_PATCH with matching grant
  ↓
candidate materialization
  ↓
PlanValidator
  ↓
PRE_PLAN
  ↓
CAS
~~~

Policy 不能直接修改 Proposal。
约束合并仍由类型化 reducer 完成。

### 9.4 Revision 状态迁移

Patch 成功：

~~~text
same plan_id
old revision = N
new revision = N + 1
state.plan_revision = N + 1
state.state_version = old + 1
all pre-existing NodeExecutionState except source EXPLORATION node byte-equivalent
all prior ExplorationState except source exploration byte-equivalent
accepted_result = ResultEnvelope(
  status=SUCCESS,
  output_type="harness.plan_patch.accepted",
  bounded patch_id / old_revision / new_revision / proposal_hash payload
)
source EXPLORATION node: RUNNING -> SUCCEEDED
  result=accepted_result, error=None, completed_at=commit_time
source exploration: RUNNING -> SUCCEEDED
  final_result=accepted_result, completed_at=commit_time
  created_plan_revision unchanged
source actions / observations / model attempts byte-equivalent
new NodeExecutionState = PENDING
patch_node_origins written for every new node
revision audit appended
patch state = ACCEPTED, completed_at=commit_time
pending_patch_id cleared
matching patch ApprovalRequest / Continuation / pending approval and job indexes removed
matching typed Grant remains immutable audit and is marked consumed by this patch
Plan WAITING or mutation-suspended -> RUNNING
scheduler_generation + 1 and patch handoff claim committed
~~~

上述是 v1 唯一允许对既有 child state 的修改：完成 source exploration 与它的
outer node，并把同一 PlanPatchExecutionState 从 PROPOSED / WAITING_APPROVAL 转为
ACCEPTED。`accepted_result` 的两个持久化位置必须 canonical value / hash 相同，并与
SUCCEEDED 状态匹配；不能只改 status 而漏写 node.result / Exploration.final_result。其他历史
节点、执行结果和 provider facts 不改写。

Patch accepted 是当前 Exploration 的终态。它返回受控
harness.plan_patch.accepted ResultEnvelope；Patch 新增节点只能在新 Scheduler generation 中
运行。accept CAS 成功后必须从 SQLite / StateStore 重载并先通过 RecoveryValidator；只有
重载记录中的 Plan WAITING 已解除、matching pending ref 已失效且 source result 可用于 binding，
新 tail 才能 admission。迟到 Grant 只能命中已消费的 audit，不能再次进入 PRE_PATCH。

Patch 技术性失败（invalid / conflict）：

~~~text
plan revision unchanged
state version 仅在记录“Patch rejected observation”时正常递增
patch state = REJECTED / CONFLICTED
pending_patch_id cleared
no new nodes
no scheduler visibility
~~~

recoverable Patch reject 使用 Observation(source_ref=PlanPatchRef) 返回当前 Exploration；
若 Budget 仍允许，模型可以选择 Finish 或其他 Action，但必须由新 Scheduler
generation 恢复。Policy / human rejection 不产生可继续 Observation。Patch approval waiting
则保持同一 persisted PlanPatchExecutionState，不重新生成 Draft。

Governed rejection（PRE_PATCH / PRE_PLAN DENY、PRE_PLAN approval unsupported 或 human
rejection）也有完整的单 CAS postcondition：

~~~text
denied_result = ResultEnvelope.denied(
  ErrorDetail(
    code="HARNESS.PLAN_PATCH.DENIED",
    bounded patch_id / proposal_hash / policy reason details
  )
)
no output / continuation
patch = REJECTED, error=the same canonical governance_error, completed_at=commit_time
source Exploration = DENIED, final_result=denied_result, completed_at=commit_time
outer EXPLORATION node = DENIED, result=denied_result, completed_at=commit_time
Plan = DENIED, matching issue + completed_at
pending_action_id / pending_patch_id cleared
all plan-level pending approvals / jobs / continuations removed or terminal-invalidated
matching Grant / policy decision retained as immutable audit and marked consumed
scheduler_generation + 1; no scheduler / model / Provider handoff
~~~

Node / Exploration / Plan 的 terminal status、ResultEnvelope status、error、issue 与 timestamps 必须
通过 RecoveryValidator 的现有终态不变量。不得只写 Patch=REJECTED 后留下 outer node RUNNING，
也不得让其他 pending callback 把 terminal Plan 恢复。

### 9.5 Patch 与 Workflow

Patch 是同一次执行中的受治理修订，不是对长期模板的修改。

~~~text
PlanPatch
  ≠ Workflow publish
  ≠ StaticPlanner route mutation
  ≠ Catalog update
~~~

---

## 10. Error Model

新增错误码必须保持：

~~~text
stable code
safe summary
bounded structured details
no raw provider / model message
retryable / fallbackable 语义明确
~~~

### 10.1 Routing / availability

~~~text
HARNESS.ROUTE.NOT_APPLICABLE
HARNESS.ROUTE.MODE_NOT_AVAILABLE
HARNESS.ROUTE.INVALID_PROPOSAL
HARNESS.ROUTE.PROPOSAL_KIND_NOT_ALLOWED
HARNESS.ROUTE.MATERIALIZATION_FAILED
HARNESS.ROUTE.INVALID_PRIMARY
~~~

NOT_APPLICABLE 是唯一允许 RoutingPipeline 进入模型 fallback 的 primary 结果。

### 10.2 Structured output

~~~text
HARNESS.MODEL.STRUCTURED_OUTPUT_UNSUPPORTED
HARNESS.MODEL.STRUCTURED_OUTPUT_SCHEMA_INVALID
HARNESS.MODEL.STRUCTURED_OUTPUT_INVALID
HARNESS.MODEL.STRUCTURED_OUTPUT_TRUNCATED
HARNESS.MODEL.REFUSED
HARNESS.MODEL.CONTENT_FILTERED
HARNESS.MODEL.ACCOUNTING_INCOMPLETE
HARNESS.MODEL.RESERVATION_INVALID
HARNESS.MODEL.RESERVATION_CONFLICT
HARNESS.MODEL.RECEIPT_MISMATCH
HARNESS.MODEL.GENERATION_ORPHANED
~~~

STRICT_REQUIRED 且无 eligible Provider 时，在任何网络 / Provider 调用前失败。

### 10.3 Exploration

~~~text
HARNESS.EXPLORATION.NOT_CONFIGURED
HARNESS.EXPLORATION.INVALID_PROFILE
HARNESS.EXPLORATION.INVALID_DECISION
HARNESS.EXPLORATION.DECISION_REPAIR_EXHAUSTED
HARNESS.EXPLORATION.BUDGET_EXHAUSTED
HARNESS.EXPLORATION.BUDGET_ACCOUNTING_UNAVAILABLE
HARNESS.EXPLORATION.ACTION_INVALID
HARNESS.EXPLORATION.ACTION_NOT_ALLOWED
HARNESS.EXPLORATION.INPUT_SCHEMA_INVALID
HARNESS.EXPLORATION.REPEATED_ACTION
HARNESS.EXPLORATION.RECURSION_LIMIT
HARNESS.EXPLORATION.RESUME_UNSAFE
HARNESS.EXPLORATION.STATE_CONFLICT
HARNESS.EXPLORATION.OPERATION_CLAIM_CONFLICT
HARNESS.EXPLORATION.OPERATION_ID_REUSE
HARNESS.EXPLORATION.EVIDENCE_INVALID
HARNESS.EXPLORATION.APPROVAL_BINDING_MISMATCH
~~~

### 10.4 Plan Patch

~~~text
HARNESS.PLAN_PATCH.NOT_ALLOWED
HARNESS.PLAN_PATCH.INVALID
HARNESS.PLAN_PATCH.HISTORY_MUTATION
HARNESS.PLAN_PATCH.SCOPE_ESCALATION
HARNESS.PLAN_PATCH.BUDGET_ESCALATION
HARNESS.PLAN_PATCH.REVISION_CONFLICT
HARNESS.PLAN_PATCH.DENIED
HARNESS.PLAN_PATCH.APPROVAL_REQUIRED
HARNESS.PLAN_PATCH.STORE_CAS_UNSUPPORTED
HARNESS.PLAN_PATCH.CAS_FAILED
HARNESS.PLAN_PATCH.QUIESCE_TIMEOUT
HARNESS.PLAN_PATCH.ORIGIN_INVALID
HARNESS.PLAN_PATCH.DEADLINE_EXCEEDED
HARNESS.PLAN_PATCH.DESCRIPTOR_CHANGED
~~~

### 10.5 Identity

~~~text
HARNESS.PLAN.IDENTITY_GENERATION_FAILED
HARNESS.PLAN.TEMPLATE_INVALID
HARNESS.PLAN.EXECUTION_ID_CONFLICT
~~~

---

## 11. 模块修改范围

### 11.1 harness-contracts

新增 / 修改：

~~~text
PlanNodeKind.EXPLORATION
ExplorationNodeSpec
ExplorationBudget
ExplorationBudgetTemplate
ExplorationBudgetCeiling
ExplorationUsage
ExplorationPermissions
PatchExecutionLimits
ExplorationPolicyConstraints
ExplorationStatus
ActionProposal
ActionExecutionStatus
ActionExecutionState
ProviderSelectionIntent
Observation
ExplorationState
ExplorationModelAttemptState
ModelAttemptSlotExecutionState
ExecutionUnitRef variants
PlanPatchProposal
PlanPatchBudgetReservation / PlanPatchExecutionLedger
PlanPatchExecutionState
PatchNodeOrigin
PlanRevisionAudit
PlanExecutionProfile
StructuredOutputSpec
ModelProviderFeatures
ModelGenerationAttemptSlot / ModelGenerationReservation / ModelReservationReceipt
NormalizedCost / NormalizedCostRate
ModelUsage / ModelAttemptAccounting / ModelProviderAttemptUsage / ModelGenerationAccounting
Continuation.execution_ref
ApprovalRequest / Grant execution_ref + proposal/provider/policy binding
BoundApprovalState
PlanExecutionState.explorations
PlanExecutionState.revision_history
PlanExecutionState.scheduler_generation / claimed_operation / external_operation_history
ExternalOperationInput / Claim / Record / Outcome
ExecutionTransitionDelta / AppliedExecutionTransition
PlanExecutionState.patch_node_origins / applied_transitions
~~~

兼容要求：

- 新字段提供安全默认值；
- 旧 Request / Plan / Checkpoint 仍可反序列化；
- 旧 PlanNode JSON 不受新增 enum 影响；
- plan_id wire 名称不改变；
- ResultEnvelope 状态语义不改变。

### 11.2 harness-model

新增：

~~~text
StructuredGenerationAdapter
full local JSON Schema validator
provider feature eligibility
GenerateRequest.structured_output
GenerateResult.accounting
ModelProvider.features / prepare_structured_output / generate_prepared
ModelProvider.bound_input_tokens
ModelGateway.prepare_generation / execute_prepared
PreparedModelGeneration / reservation builder
per-slot STARTED / terminal checkpoint fencing
ModelAccountingAccumulator before ResultEnvelope conversion
strict unsupported policy
finish reason / refusal normalizer
schema hash
~~~

StructuredGenerationAdapter 是 Router / Planner / Explorer 共用的生成助手，不是新的 Provider SPI。

ModelProvider 仍是唯一厂商模型边界。

### 11.3 harness-registry

新增 / 修改：

~~~text
registration-time immutable ModelProviderFeatures snapshot
feature hash / schema-preparation eligibility lookup
legacy Provider safe all-false strict defaults
~~~

Registry 不编译 Schema，也不信任 request metadata 声明的 feature；具体 Schema 是否可
无损编译仍由 ModelGateway 调用受信任 Provider adapter 判定。

### 11.4 harness-routing

新增 / 修改：

~~~text
RoutingPipeline
RouterNotApplicableError
CapabilitySelectionDraft
AutoRouteDraft union
RouteDecisionMaterializer
RouteAvailability
LLMRouter route-v2
RoutingContext.effective_mode
~~~

RuleRouter：

- 保持确定性规则；
- fixed PLAN / EXPLORE / HYBRID 不调用模型；
- fixed FAST 无 target/rule 时只能抛 RouterNotApplicableError；
- legacy internal fallback 必须在 Composition Root 拆到 RoutingPipeline；
- RoutingPipeline 拒绝带 internal fallback 的 primary；
- 不内置反向猜测。

LLMRouter：

- Prompt 不包含 requested/effective mode；
- fixed FAST 只返回 CapabilitySelectionDraft；
- AUTO 返回按 availability 收窄的 Route Draft；
- 不允许模型输出 final control fields。

### 11.5 harness-spi

新增中性 Harness 协作面：

~~~text
ExplorationNodeExecutorProtocol
ExplorationCheckpointSink
PlanMutationHandoff
ProviderExecutionCheckpointSink
model reservation / slot transition methods on ExplorationCheckpointSink
~~~

`PlanMutationDirective / PlanMutationSuspension` 是可序列化的 harness-contracts outcome；
Protocol 只定义调用方向，不暴露 StateStore / CapabilityInvoker 服务实例。

依赖 DAG 冻结为：

~~~text
harness-contracts <- harness-spi
harness-contracts / harness-spi <- harness-runtime
harness-contracts / harness-spi <- harness-execution
harness-contracts / harness-spi <- harness-model / harness-planning / harness-policy
harness-agentic -> harness-model + harness-runtime + harness-planning + harness-policy
harness-agentic -> harness-contracts + harness-spi
harness-agentic  -X-> harness-execution
harness-execution -X-> harness-agentic
harness-bootstrap -> runtime + execution + agentic（唯一组装点）
~~~

ExecutionEngine 只依赖注入的 ExplorationNodeExecutorProtocol 并拥有
PlanCheckpointCoordinator / Scheduler handoff。harness-agentic 只调用注入的
ExplorationCheckpointSink / PlanMutationHandoff，不 import harness-execution；它可以正向依赖
ModelGateway、CapabilityInvoker、PlanPatchValidator 与 Policy 的公开边界。这些模块均不反向
import harness-agentic，因此不构成环。

### 11.6 harness-planning

新增 / 修改：

~~~text
PlanTemplate
PlanIdentityFactory
PlanMaterializer
PlannerOutputNormalizer
Planner.plan_artifact() compatibility default
PlanNodeDraft
Coordinator-owned exactly-once fresh materialization
LLMPlanner planner-v2
HYBRID allowed node kinds
PlanPatchValidator
PlanPatchMaterializer
~~~

PlanValidator 增加：

~~~text
EXPLORATION node shape
mode-specific node kind availability
profile / scope consistency
patch append-only candidate validation
new node and edge limits
~~~

Planner SPI 仍然保留：

~~~python
async def plan(context: PlanningContext) -> ExecutionPlan
~~~

并新增具有 concrete legacy default 的 `plan_artifact() -> PlanTemplate | ExecutionPlan`。
Planner 返回的 ExecutionPlan 是 candidate；RequestCoordinator 不信任它的 identity，
必须通过 Normalizer → Materializer。`execute_plan()` 是明确 bypass。

### 11.7 harness-agentic（新增）

建议目录：

~~~text
harness-agentic/
  src/harness_agentic/
    __init__.py
    context.py
    profiles.py
    budget.py
    drafts.py
    materialization.py
    validation.py
    observations.py
    executor.py
    engine.py
    node_executor.py
    patch.py
    eventing.py
~~~

公共边界：

~~~text
ExplorationEngine
ExplorationProfile
ExplorationProfileRegistry（Composition-time, read-only）
ScopedActionExecutor
ActionValidator
ObservationProjector
ExplorationNodeExecutor
PlanPatchCoordinator
~~~

不导出：

~~~text
CapabilityInvoker instance
Registry instance
Provider instance
StateStore mutation helper
PolicyEngine service locator
~~~

### 11.8 harness-policy

新增：

~~~text
PolicyPhase.PRE_PATCH
ExplorationPolicyConstraintReducer for PRE_ROUTE
RoutePolicyConstraints.exploration
PlanningConstraints.exploration handoff
PlanPatchPolicyContext
PlanPatchPolicyConstraints
PlanPatchConstraintReducer
resolve_pre_patch_policy
~~~

保持：

~~~text
Action real invocation → PRE_EXECUTE
Route governance → PRE_ROUTE
candidate / revised plan → PRE_PLAN
~~~

PRE_ROUTE Approval 继续 fail-closed，不借 3C 偷渡 Request-level waiting。
PRE_PLAN 只审批最终已物化 Plan，不使用 opaque constraints 就地修改它。

### 11.9 harness-runtime

CapabilityInvoker / ProviderExecutionCoordinator 必须重构为下列 additive 内部阶段：

~~~text
select candidate Provider
  ↓
Invoker authorization hook:
  on_provider_selected -> checkpoint ProviderSelectionIntent
  -> PRE_EXECUTE(selected Provider, optional matching Grant)
  ├── WAITING / DENY: no ProviderAttempt, no outbound
  └── ALLOW: PreparedProviderInvocation
  ↓
ProviderExecutionCoordinator attempt_started callback/checkpoint
  ↓
outbound invoke_selected（transport only; no hidden Policy）
~~~

内部 additive 接口冻结为 `authorize_selected_provider(selection, execution_binding,
approval_grant=None) -> PreparedProviderInvocation | WAITING`。`PreparedProviderInvocation` 绑定
provider_id、selection_key、proposal_hash、policy_decision_hash 与 invocation fingerprint，不对
Plugin 导出。旧 `CapabilityInvoker.invoke()` 签名保持；非 Exploration 调用使用安全
default checkpoint hook，仍按同一 PRE_EXECUTE-before-attempt 顺序执行。

调用面使用兼容的可选 keyword-only binding：

~~~python
class ProviderExecutionCheckpointSink(Protocol):
    async def provider_selected(self, fact: ProviderSelectionIntent) -> None: ...
    async def attempt_started(self, fact: ProviderAttempt) -> None: ...
    async def attempt_completed(
        self,
        fact: ProviderAttempt,
        result: ResultEnvelope,
    ) -> None: ...

@dataclass(frozen=True)
class InvocationExecutionBinding:
    execution_ref: ExecutionUnitRef
    proposal_hash: str
    approval_grant: ApprovalGrant | None
    checkpoint_sink: ProviderExecutionCheckpointSink

async def invoke(
    capability_id: str,
    input: RequestInput,
    context: InvocationContext,
    *,
    # plugin_id / timeout / deadline / retry / callbacks / resume / parent 等旧 kwargs 全部保留
    execution_binding: InvocationExecutionBinding | None = None,
) -> ResultEnvelope: ...
~~~

ScopedActionExecutor 注入一个包装 PlanCheckpointCoordinator 的 sink；harness-runtime
不持有 StateStore，也不 import harness-agentic。sink 任何 checkpoint 失败都必须在
outbound 前向上抛，不得仅记 Event 后继续。`execution_binding=None` 保持 Stage 1/2/3A
旧调用，但仍经过 PRE_EXECUTE。
为避免同一 checkpoint 被写两次，execution_binding.checkpoint_sink 与旧的 raw
`attempt_started/attempt_completed` callbacks 不得同时由不同对象接管；Invoker
在入口校验该互斥性。

每次 same-provider retry 可复用已授权的 exact PreparedProviderInvocation；任何 cross-provider
fallback 都必须重新执行 provider-selected checkpoint 和 PRE_EXECUTE，不得把前一个
Provider 的 Grant 或 policy hash 转移给新 Provider。Approval resume 必须 pin persisted
provider_id，不重新自由 selection。

### 11.10 harness-execution

修改：

~~~text
Scheduler dispatch EXPLORATION node
PlanMutationSuspension
Exploration child state checkpoint callback
approval coordinator action / patch refs
approval proposal/provider/policy hash binding
async completion action-level delegation
recovery of proposed / waiting action
ExplorationRecoveryValidator / ResumeCoordinator branch
persisted cancellation of WAITING action / patch
ProviderSelectionIntent before PRE_EXECUTE
ProviderAttempt creation only after PRE_EXECUTE ALLOW
revision-aware scheduler continuation
plan-level lock for Patch
CAS checkpoint integration
PlanCheckpointCoordinator for every EXPLORATION-containing record write
scheduler generation handoff
~~~

关键要求：

- EXPLORATION node 的内部 Action completion 不等于 Node completion；
- child terminal checkpoint 与 outer node completion 使用同一 Plan record；
- resume 不重新 Route / Plan；
- proposal 已持久化后不重新模型决策；
- revised Plan checkpoint 后才对 Scheduler 可见。
- 旧 Scheduler generation 的迟到 callback 不能覆盖 revised record。

#### CAS transition ownership

所有含 EXPLORATION node 的 record 禁止 Scheduler / callback 先修改共享
PlanExecutionState、调用 `_touch()`，再把 whole-record snapshot 交给 Store。callback 只能提交
immutable `ExecutionTransitionDelta`：

~~~text
transition_id / fact_hash
captured plan_revision / scheduler_generation / owner_epoch
execution_ref + expected target status / attempt or slot id
one typed fact（proposal, provider selection, attempt start/result, observation,
                model reservation/slot, approval, cancel, patch, terminal bundle）
~~~

PlanCheckpointCoordinator 是唯一 version owner。它在 per-plan lock 内 load latest，验证 delta 的
revision / generation / owner epoch、target 前置状态和 transition_id，然后在 latest 的私有 copy
上只应用这一条语义 transition，追加 AppliedExecutionTransition，并唯一设置
`state_version = latest.state_version + 1`，最后 compare-and-save。一次 terminal bundle 可以原子
更新 child / outer / Plan 多个相互依赖字段，但仍只是一条 delta / 一个 version。

跨进程 CAS conflict 后只允许有界 reload：仅当 revision / generation / owner epoch 未变、同一
transition_id 尚未应用且 target 前置状态仍成立，才把同一 immutable delta 应用到新 latest。
跨 revision / generation 不 rebase；target 已改变时返回 duplicate outcome 或稳定 conflict，不能
重新消费 callback。`applied_transitions` 使相同 transition_id + fact_hash 幂等；相同 ID + 不同
hash 是 corruption。这样任何 save 都严格 +1，不会把并发 callback 偷偷合并成一次跳版本快照。

非 EXPLORATION 的 legacy Plan 可继续使用原 Scheduler mutable path；一旦
PlanExecutionProfile.checkpoint_cas_required=true，必须切换到上述 delta path，不能混用。

### 11.11 harness-state

StateStore 新增可选能力：

~~~python
async def compare_and_save(
    self,
    record: PlanExecutionRecord,
    *,
    expected_state_version: int,
) -> None
~~~

该方法不能作为新 abstractmethod 破坏旧第三方 Store。StateStore base class 提供
可实例化的 concrete default，默认抛稳定 `HARNESS.STATE.CAS_UNSUPPORTED`；也可以
暴露 `supports_compare_and_save=false` 的 capability flag 供执行前检查。旧 subclass 不需修改
即可继续实例化并运行 Direct / 普通 PLAN。

语义：

- 只有存储中 state_version 等于 expected 时更新；
- 更新后 record.state_version 必须精确等于 `expected_state_version + 1`；
- 不匹配抛稳定 conflict；
- payload 与 state_version 原子替换。

内置：

~~~text
InMemoryStateStore → lock + expected check
SQLiteStateStore → UPDATE ... WHERE plan_id=? AND state_version=?
~~~

旧第三方 Store：

- 普通 create/load/save/delete 不变；
- 未实现 CAS 时，任何 EXPLORE / HYBRID / 含 EXPLORATION node 的 execute_plan
  都在首次模型或业务调用前 fail-closed；
- 普通不含 EXPLORATION node 的 PLAN 仍可工作；
- 不允许用 load + unconditional save 假装 CAS。

### 11.12 harness-bootstrap

ApplicationComponents 新增：

~~~text
routing_pipeline
route_availability
plan_materializer
exploration_profiles
exploration_engine
exploration_plan_factory
plan_patch_coordinator
~~~

RequestCoordinator._dispatch 必须穷举：

~~~text
FAST    → Runtime / CapabilityInvoker
PLAN    → trusted Planner → ExecutionEngine
EXPLORE → ExplorationPlanFactory → ExecutionEngine
HYBRID  → trusted hybrid-capable Planner → ExecutionEngine
~~~

禁止使用：

~~~text
if FAST else Planner
~~~

Mode availability：

~~~text
FAST    → 基础可用
PLAN    → configured Planner
EXPLORE → configured Profile + ExplorationEngine + CAS-capable StateStore
HYBRID  → configured hybrid Planner + Profile + EXPLORATION node support + CAS-capable StateStore
~~~

未配置组件时保持已冻结的稳定错误语义：

~~~text
PLAN planner 未配置       -> HARNESS.PLANNER.NOT_CONFIGURED（保持 3B）
EXPLORE/HYBRID 组件不完整 -> HARNESS.ROUTE.MODE_NOT_AVAILABLE
~~~

RouteAvailability 只用于在 AUTO Draft schema 中排除当前不可用的 route kind，不得
把最终 dispatch 时的 Planner-specific 稳定错误统一改写成 MODE_NOT_AVAILABLE。

### 11.13 harness-trace / harness-events

新增 SpanType：

~~~text
EXPLORATION
PLAN_PATCH
~~~

Action 不必为每个瞬时状态增加新 SpanType。

推荐结构：

~~~text
REQUEST
  └── RUNTIME
       ├── ROUTE
       │    └── MODEL（仅 fallback）
       └── PLAN
            └── EXPLORATION
                 ├── MODEL
                 ├── CAPABILITY
                 │    └── PROVIDER_SELECT / AGENT / TOOL
                 └── PLAN_PATCH
                      └── POLICY
~~~

新增 Events：

~~~text
exploration.started
exploration.decision_completed
exploration.action_proposed
exploration.action_started
exploration.action_waiting
exploration.action_completed
exploration.observation_created
exploration.resumed
exploration.completed
exploration.budget_exhausted
exploration.repeated_action

plan_patch.proposed
plan_patch.waiting
plan_patch.accepted
plan_patch.rejected
plan_patch.conflicted
~~~

### 11.14 根项目与测试

新增：

~~~text
harness-agentic package mapping
harness-agentic/README.md
harness-agentic/tests
tests/stage3c
tests/stage3c/README.md
~~~

如引入完整 JSON Schema validator，依赖必须：

- 固定兼容范围；
- 禁止远程 schema resolution；
- 在 lock / 安装说明中更新；
- 对恶意深层 schema 有资源上限测试。

---

## 12. 推荐实施步骤

### Step 1 — Plan Identity / Materialization 收口

#### 目标

修复任意 Planner 输出可复用 plan_id 的真实缺口，并冻结唯一身份 trust boundary。

#### 实施内容

1. 新增 PlanIdentityFactory。
2. 新增正式、无运行身份的 PlanTemplate Contract；PlanDefinition 名称保留给未来
   Workflow 定义层，不在 3C 混用。
3. 新增 PlannerOutputNormalizer / PlanMaterializer。
4. Planner base 新增 concrete-default plan_artifact()，旧 Planner 不破坏。
5. RequestCoordinator 对所有 Planner artifact 恰好执行一次 Normalizer → Materializer。
6. StaticPlanner / LLMPlanner 覆写 plan_artifact() 返回 PlanTemplate，移除最终 identity ownership。
7. 新增恶意/错误自定义 Planner 重复返回同 ExecutionPlan 的 fixture。
8. execute_plan 保持具体 plan identity 语义并 bypass Materializer。
9. 文档明确 plan_id / revision / workflow identity。

#### 完成标准

- 相同 Static template 连续执行两次，plan_id 不同；
- custom Planner 连续返回同一 plan_id 时，两次 handle 的最终 plan_id 仍不同；
- 两次均可创建 StateStore record；
- resume 保持原 plan_id；
- 模型不能注入 plan_id / revision；
- Stage 2 execute_plan 相同 plan_id 重复创建仍明确冲突；
- 旧 Static factory 用法兼容。

---

### Step 2 — Strict Structured Output Foundation

#### 目标

建立 Router / Planner / Explorer 共用的可靠结构化生成协议。

#### 实施内容

1. StructuredOutputSpec。
2. GenerateRequest.structured_output / GenerateResult.accounting。
3. ModelProviderFeatures / prepare_structured_output / Registry feature snapshot。
4. ModelProvider.bound_input_tokens 与可信 normalized cost upper bound。
5. ModelGenerationAttemptSlot / Reservation / Receipt。
6. ModelGateway prepare_generation → reserve CAS → execute_prepared 两阶段 API。
7. 每个 retry / fallback slot 的 STARTED / terminal checkpoint fencing。
8. Provider eligibility 与具体 Schema 无损编译。
9. 完整本地 JSON Schema 校验。
10. schema resource limits / remote ref rejection。
11. refusal / truncated / content filter 归一化。
12. ModelAccountingAccumulator / StructuredGenerationAdapter。
13. LLMPlanner-v2 迁移到 identity-free PlanDraft / PlanNodeDraft strict schema。
14. Mock strict / legacy / fallback-accounting Provider。

#### 完成标准

- REQUIRED Provider 不支持时零 generation；
- 不静默降级；
- nested type / enum / additionalProperties 被正确校验；
- max tokens / refusal / filter 不进入下游 Draft parser；
- schema hash 可观察，完整 schema / prompt / raw response 不可观察；
- 首 Provider 消耗后失败、fallback 成功时，token/cost 聚合包含两次 attempt；
- reservation 覆盖所有允许 slots 的 sound input/output token 与 normalized cost 上界；
- 无 reservation receipt / slot STARTED CAS 时 Provider outbound=0；
- cancel / handoff 抢先后旧 receipt 失效，fallback 也不能绕过 fencing；
- RESERVED / RUNNING crash 后整体 ORPHANED，同一 generation / slot 不跨进程重放；
- LLMPlanner-v2 schema 拒绝 plan_id/revision/retry/idempotency 等受信任字段；
- legacy JSON best-effort Gate 继续通过。

---

### Step 3 — RoutingPipeline / route-v2

#### 目标

固化 deterministic-first，并让模型只生成未知路由字段。

#### 实施内容

1. RouterNotApplicableError。
2. RoutingPipeline。
3. RouteAvailability。
4. RoutingContext.effective_mode。
5. CapabilitySelectionDraft / AutoRouteDraft。
6. RouteDecisionMaterializer。
7. LLMRouter route-v2。
8. Composition Root 安全装配。
9. RuleRouter legacy internal fallback 拆分 / primary rejection。

#### 完成标准

- static hit 时 model calls=0；
- 只有 typed NOT_APPLICABLE 才 fallback；
- invalid / deny / timeout 不 fallback；
- fixed PLAN / EXPLORE / HYBRID model calls=0；
- fixed FAST 模型只输出 capability；
- AUTO 模型不能输出 mode / source / explorer_id；
- Prompt 不包含 requested/effective mode；
- legacy RuleRouter(fallback=model) 静态命中时 model calls=0，未命中仅 fallback 一次；
- 默认无模型装配继续工作。

---

### Step 4 — Exploration Contracts / Persisted State

#### 目标

先冻结可序列化、可恢复的 Exploration execution truth。

#### 实施内容

1. ExplorationProfile / registry。
2. Budget Template / Budget / Usage / typed Policy constraints。
3. Turn Draft 判别联合。
4. ActionProposal / ActionState / ProviderSelectionIntent。
5. Observation。
6. ExplorationState。
7. ExecutionUnitRef。
8. Model Reservation / slot state / ExplorationModelAttemptState。
9. PlanPatchExecutionState / PatchNodeOrigin / per-node ledger。
10. PlanExecutionProfile。
11. PlanExecutionState.explorations / operation history / applied transitions。
12. Continuation / Approval additive compatibility、BoundApprovalState 与 binding。
13. ExplorationRecoveryValidator invariants。

#### 完成标准

- 所有非法判别组合被拒绝；
- 模型字段无法携带控制 identity；
- 旧 checkpoint 缺少 exploration 字段时仍可恢复；
- Exploration action ref 与 plan/node/exploration/action 一致；
- 状态 JSON round-trip；
- 预算计数单调且不能负数或回退。
- 损坏 pending/ref/hash/provider/revision 一致性的 checkpoint 在 resume 前 fail-closed。

---

### Step 5 — Exploration Decision Loop

#### 目标

实现无执行权的 bounded model decision loop。

#### 实施内容

1. bounded prompt projector。
2. dynamic strict schema。
3. prepare_generation → exact reservation CAS → execute_prepared。
4. bounded decision repair。
5. final / evidence validator。
6. repeated-action fingerprint。
7. usage accounting。
8. budget guard。

#### 完成标准

- 每 turn 精确一个 Draft；
- invalid → bounded repair；
- repair exhausted 零 Action；
- model fallback 仍由 ModelGateway 管理；
- token / model call / deadline 消耗正确；
- 每个 slot outbound 前有最新 generation/owner fencing CAS；
- crash 后旧 generation 只 ORPHANED，不 reprepare / replay；
- Observation 之外的 raw result 不进入 Prompt；
- 不保存隐藏推理。
- Step 5–10 的 dynamic schema 不包含 PlanPatchDraft，且所有 profile `allow_patch=false`。

---

### Step 6 — ScopedActionExecutor

#### 目标

把模型 Action 接到现有可靠 CapabilityInvoker 边界。

#### 实施内容

1. ActionMaterializer。
2. Harness-owned action_id / idempotency key。
3. Capability input schema validation。
4. scope / side-effect / egress guard。
5. proposal-before-dispatch checkpoint callback。
6. ProviderSelectionIntent checkpoint → PRE_EXECUTE → ProviderAttempt callbacks。
7. Result → Observation projector。
8. WRITE resume safety。

#### 完成标准

- 未知 / 越 scope / invalid input 零 Provider 调用；
- MODEL Capability 不能作为 Action；
- Action 每次经过 PRE_EXECUTE；
- REQUIRE_APPROVAL 之前不创建 ProviderAttempt，批准后 pin 同一 Provider；
- ProviderSelectionIntent checkpoint 失败时 provider_history 为空且 outbound=0；
- Policy WAITING 时 provider_history 为空；cross-provider fallback 为新 Provider 重跑
  selection checkpoint + PRE_EXECUTE；
- READ / WRITE fallback 继续符合 3A；
- 模型 idempotency 字段被拒绝；
- checkpoint 失败时零业务调用；
- proposal checkpoint 后 crash 不重新决策。

---

### Step 7 — EXPLORATION Plan Node

#### 目标

把 ExplorationEngine 作为 ExecutionEngine 的 Harness-owned 节点执行器。

#### 实施内容

1. PlanNodeKind.EXPLORATION。
2. ExplorationNodeSpec / neutral executor + checkpoint + mutation Protocols。
3. PlanValidator 扩展。
4. Scheduler protocol-only dispatch。
5. ExplorationNodeExecutor。
6. Node input binding。
7. Result / WAITING 映射。
8. same-record child state checkpoint。
9. package import-cycle smoke tests。

#### 完成标准

- EXPLORATION 不是 Registry Capability；
- 普通 PLAN 默认不能偷偷包含 exploration node；
- HYBRID / wrapper Plan 可以显式允许；
- child action 完成不会提前完成 outer node；
- Exploration terminal 与 outer node terminal 使用同一 CAS，不存在 child-terminal /
  outer-running 稳定 checkpoint；
- final result 正确成为 node ResultEnvelope；
- downstream binding 可以读取探索节点最终输出。
- harness-agentic 与 harness-execution 可分别独立 import，只由 bootstrap 组装。

---

### Step 8 — Approval / Async / Restart

#### 目标

让 Exploration WAITING 与跨进程恢复达到 Stage 2 一致性要求。

#### 实施内容

1. action-level ApprovalRequest / Grant。
2. BoundApprovalState 与 proposal/provider/policy hash binding。
3. resolve_approval 的 typed Decision/Grant + pending-index + operation claim 原子 CAS。
4. Async continuation action ref。
5. complete_async_node optional execution_ref / expected_job_ref。
6. durable ExternalOperationClaim / history / lease takeover fencing。
7. resume proposed / waiting action。
8. provider resume state reuse。
9. persisted action-level cancellation。
10. SQLite restart / duplicate worker tests。

#### 完成标准

- approval resume 同一 action；
- async completion 先形成 Observation，再继续模型循环；
- restart 不重新 Route / Plan；
- checkpointed Proposal 不被新模型决策替换；
- completed action 不重复；
- ambiguous non-idempotent WRITE fail-closed；
- approval reject 终止 Exploration，stale grant / provider substitution fail-closed；
- approval CAS 后、PRE_EXECUTE 前 crash 复用 exact typed Grant，不重复审批；
- claim owner 在 handoff 前 crash 后可按 persisted input 接管，旧 owner callback 被 fencing；
- restart 后 cancel WAITING Action 可持久收敛，迟到 approval/job 不能恢复；
- 旧 CAPABILITY node Approval / Async Gate 不回归。

---

### Step 9 — handle EXPLORE Path

#### 目标

开放显式 EXPLORE，同时保持默认未配置时 fail-closed。

#### 实施内容

1. ExplorationPlanFactory。
2. standalone single-node Plan。
3. Coordinator EXPLORE branch。
4. Route availability。
5. profile / policy selection。
6. result metadata。

#### 完成标准

- 未配置 profile / engine 时 MODE_NOT_AVAILABLE；
- 配置后显式 EXPLORE model Router calls=0；
- fresh plan_id / exploration_id；
- 统一 REQUEST lifecycle；
- WAITING 返回 plan-controllable continuation；
- invoke / execute_plan 语义不变。
- Step 11 完成前 standalone EXPLORE 固定 allow_patch=false。

---

### Step 10 — HYBRID Path

#### 目标

允许受信任 Planner 生成包含明确 Exploration 节点的 Plan。

#### 实施内容

1. HYBRID-specific PlanningConstraints。
2. PlanNodeDraft exploration variant。
3. PlanMaterializer profile injection。
4. trusted hybrid_planner_id。
5. Coordinator HYBRID branch。
6. downstream result binding。
7. HybridPlanner 名称消歧文档。

#### 完成标准

- explicit HYBRID Router model calls=0；
- model 不能选择 planner / explorer profile；
- PlanValidator 限制 exploration node；
- Plan → Explore → downstream 成功；
- Explore WAITING 传播为 Plan WAITING；
- resume 不重新 Planner / Explorer 已完成 Action。
- Step 11 完成前 HYBRID Plan 固定 patch_enabled=false / allow_patch=false。

---

### Step 11 — PRE_PATCH / Plan Revision / CAS

#### 目标

实现受治理、append-only、原子可恢复的 PlanPatch。

#### 实施内容

1. PlanPatchDraft / Proposal / Audit。
2. PlanPatchValidator。
3. PRE_PATCH Policy。
4. PlanPatchMaterializer。
5. built-in StateStore CAS。
6. PlanCheckpointCoordinator。
7. immutable transition delta / single version owner / cross-process CAS retry rules。
8. patch-capable Exploration execution-wide barrier。
9. Scheduler quiesce / PlanMutationSuspension / generation handoff。
10. PlanPatchCoordinator。
11. PlanPatchExecutionState / PatchNodeOrigin / per-node ledger / state migration。
12. absolute deadline admission + every-outbound guard / descriptor revalidation。
13. revision-aware Scheduler。
14. patch approval typed Grant exact binding + SQLite restart。
15. persisted WAITING patch cancellation / late Grant invalidation。
16. conflict / QUIESCE_TIMEOUT terminal handling。

#### 完成标准

- legal append-only patch revision 精确 +1；
- plan_id 不变；
- 除 source EXPLORATION node / ExplorationState 的受限终态迁移外，所有既有状态
  byte-equivalent；
- invalid / denied / scope escalation 零新增执行；
- two concurrent patches 最多一个 CAS 成功；
- ordinary checkpoint 与 Patch CAS 竞态不会发生旧 revision 覆盖；
- unsupported Store fail-closed；
- CAS checkpoint 后 restart 恢复新 revision；
- every CAS transition 精确 version+1，Scheduler 不预改共享 execution truth；
- RevisionAudit 持久化；
- quiesce 之前的 proposal_state_version 不被错当 CAS expected version；
- source created_plan_revision 保持原值，历史 ExplorationState 可通过 revision 一致性校验。
- REQUIRE_APPROVAL 在 SQLite restart 后使用同一 patch_id / proposal_hash，重跑完整
  PRE_PATCH / validator / PRE_PLAN 后才 CAS；
- mismatched/stale Grant 零 mutation，WAITING Patch 取消后迟到 Grant 不能恢复执行。
- accept CAS 同时保存 SUCCESS accepted result、child/outer timestamps、清除 matching pending
  indexes，reload 后 source binding 可用；
- governed reject 同时保存 DENIED child/outer/Plan result / issue / timestamps；
- Patch-added node 只能凭 persisted origin 消费同一 ledger/deadline，Catalog 漂移或 deadline
  过期时零 outbound；
- 非 barrier 的 allow_patch Plan 在 validation 阶段失败，QUIESCE_TIMEOUT 不启动新 Scheduler。

---

### Step 12 — Observability

#### 目标

建立 Request → Plan → Exploration → Action → Provider → Patch 的安全关联。

#### 实施内容

1. EXPLORATION / PLAN_PATCH Span。
2. Explore / Action / Patch Events。
3. IDs / hashes / counters。
4. usage / budget attributes。
5. waiting / resumed events。
6. redaction tests。

#### 完成标准

- 单 handle 仍只有一个 REQUEST / RUNTIME 根；
- Action MODEL / CAPABILITY / PROVIDER span 正确挂接；
- plan_id / node_id / exploration_id / action_id 可关联；
- Trace / Events 不含 raw prompt / response / output / credentials / CoT；
- checkpoint 失败不被观察面成功掩盖；
- Observability failure 不改变执行结果。

---

### Step 13 — Stage 3C Acceptance / Docs

#### 目标

建立仓库级阻断 Gate 并同步所有 Markdown 文档。

#### 实施内容

1. tests/stage3c。
2. deterministic model / capability fixtures。
3. SQLite restart / crash windows。
4. CAS concurrency。
5. Stage 1 / 2 / 3A / 3B regression。
6. README / design / ADR update。
7. Ruff / diff check。

#### 完成标准

- 所有 3C Gate 通过；
- 所有旧 Gate 通过；
- 无真实网络或真实模型依赖；
- 文档中 Stage 状态一致；
- EXPLORE/HYBRID 不再被错误标成 3D；
- 非目标未被偷偷实现。

---

## 13. 关键运行语义

### 13.1 EXPLORE 仍然是一份 Plan execution

standalone EXPLORE 被 materialize 成单节点 Plan 的目的不是把探索伪装成 Workflow，
而是确保所有有副作用、可等待的执行共享同一个 StateStore truth。

因此：

~~~text
EXPLORE fresh run → fresh plan_id
resume EXPLORE    → same plan_id
Action IDs        → child execution identities
~~~

### 13.2 模型不需要知道 mode

模型需要知道的是：

~~~text
它当前需要填写什么字段
它可以从哪些候选中选择
~~~

模型不需要知道：

~~~text
为什么 Policy 强制 FAST
调用方原始 requested mode
Harness 如何映射 mode / route_type
~~~

限制应尽量由不同 Draft Schema、候选过滤和 Materializer 表达，而不是在 Prompt 中要求模型回显控制字段。

### 13.3 EXPLORE 和 HYBRID 的区别

EXPLORE：

~~~text
单 exploration node
目标是在 bounded loop 中形成最终结果
默认不允许 PlanPatch
~~~

HYBRID：

~~~text
outer ExecutionPlan 包含 capability / approval / exploration nodes
exploration result 可供下游节点使用
受 Profile / Policy 允许时可提出 PlanPatch
~~~

第一版 standalone EXPLORE 不允许 Patch；需要动态扩展主 Plan 的请求应使用 HYBRID。

### 13.4 HybridPlanner 不等于 HYBRID mode

~~~text
HybridPlanner
  = deterministic Planner NOT_APPLICABLE 时的 fallback 组合

ExecutionMode.HYBRID
  = Plan 中包含受控 exploration node 的执行模式
~~~

两者不共享隐含选择逻辑。

### 13.5 Resume 不重新做已稳定决策

~~~text
Plan checkpoint 后
  → 不重新 Route / Plan

Action proposal checkpoint 后
  → 不重新生成该 Action

Action result checkpoint 后
  → 不重新执行该 Action

Patch CAS checkpoint 后
  → 不重新应用同一 Patch
~~~

模型 generation 完成但 Draft 尚未 checkpoint 时可以重新 generation，因为没有业务副作用；
观察面 attempt 可能重复，但执行事实不能重复。

### 13.6 Catalog / Policy 变化

恢复旧 Action：

- 依据 checkpointed Proposal 与 Provider resume facts；
- 仍执行安全性与 Tenant 一致性检查；
- 不能因新 Provider 出现而切换不安全 WRITE。

开始新 turn：

- 重新确认 scope 中 Capability 当前可执行；
- Policy 可以收紧；
- 不自动吸收新增 Capability。

### 13.7 Model failure 与 Action failure 分离

~~~text
Model failure
  → 无 Action / Provider side effect

Action failure
  → 形成 Observation 或按 failure policy 终止

Provider retry / fallback
  → 属于同一个 Action 的数据面执行
~~~

Explorer 不应因 Provider transient failure 自己重复提出相同 Action 来模拟 retry。

### 13.8 Budget 精确计数

精确计数：

- 每次逻辑 generation 先由 `prepare_generation` 冻结 exact reservation，再在
  `reserve_model_generation` CAS 中预留 `model_calls + 1` 与 reservation.total_*；
- 同一 Gateway generation 内的 Provider retry / fallback 不另加 model_calls，但所有
  attempt 的 token/cost 必须全部累加；
- crash 后 RESERVED / RUNNING generation 以 ORPHANED reservation 继续占用额度；新
  generation 再独立增加 model_calls；
- decision repair 再次 generation，model_calls +1；
- 合法 ActionProposal checkpoint 后，action_calls +1；
- Action Provider retry 不增加 exploration action_calls；
- 每次成功消费一个合法 Draft，steps +1；
- PatchProposal materialize 后，patch_count +1；
- resume 不重置任何计数。

### 13.9 Budget exhausted 的输出

已有 Observation：

~~~text
PARTIAL
bounded evidence index
BUDGET_EXHAUSTED issue
~~~

无 Observation：

~~~text
FAILED
HARNESS.EXPLORATION.BUDGET_EXHAUSTED
~~~

不得为了生成漂亮总结突破 absolute budget。

### 13.10 Patch reject 后的行为

所有拒绝都不改变 Plan，但后续语义分两类：

- recoverable structure / revision conflict：记录安全 rejection fact + Observation，由新
  Scheduler generation 恢复；若预算仍允许，Explorer 可 finish 或选择其他 Action；
- PRE_PATCH/PRE_PLAN Policy DENY、PRE_PLAN approval unsupported 或 human rejection：
  Exploration / outer node 直接受治理终止，不生成可继续 Observation，不进入新模型 turn。

同一 patch hash 重复受 repeated proposal guard；revision conflict 只能重新加载后记录
拒绝，不能盲目覆盖或 rebase application。

### 13.11 StateStore 与 Memory

ExplorationState 是 execution truth，属于 PlanExecutionRecord / StateStore。

它不是长期对话 Memory。

3D MemoryProvider 上线后也不能成为：

~~~text
Action completion truth
Approval truth
Provider resume truth
Plan revision truth
~~~

### 13.12 默认组装必须保持轻量

没有 ModelProvider、Planner、ExplorationProfile 时：

~~~text
Direct invoke 可用
explicit target FAST 可用
execute_plan 可用
EXPLORE / HYBRID fail-closed
模糊 AUTO 按配置返回 NO_MATCH
~~~

基础 Harness 不能因 3C 自动依赖模型。

---

## 14. 推荐模块实施顺序

~~~text
1. harness-contracts：identity / structured output additions
      ↓
2. harness-planning：PlanTemplate / Materializer / Static fix
      ↓
3. harness-model：strict structured output
      ↓
4. harness-routing：Pipeline / proposal / route-v2
      ↓
5. harness-contracts：exploration / action / patch state
      ↓
6. harness-agentic：profile / budget / validator / loop
      ↓
7. harness-agentic：ScopedActionExecutor
      ↓
8. harness-execution：EXPLORATION node / checkpoint / recovery
      ↓
9. harness-bootstrap：EXPLORE dispatch
      ↓
10. harness-planning + bootstrap：HYBRID
      ↓
11. harness-policy + state + execution：PRE_PATCH / CAS / revision
      ↓
12. trace / events
      ↓
13. acceptance / docs
~~~

---

## 15. 推荐 Commit 拆分

### Commit 1 — Stage 3C Identity Foundation

~~~text
PlanTemplate
PlanMaterializer
PlanIdentityFactory
StaticPlanner fresh plan IDs
identity tests
~~~

### Commit 2 — Strict Structured Output

~~~text
StructuredOutputSpec
Provider features
full local schema validation
finish reason normalization
mock provider tests
~~~

### Commit 3 — Routing Pipeline v2

~~~text
RoutingPipeline
Route drafts
RouteDecisionMaterializer
prompt route-v2
mode availability
~~~

### Commit 4 — Exploration Contracts

~~~text
Profile / Budget / Usage
Action / Observation / State
ExecutionUnitRef
checkpoint compatibility
~~~

### Commit 5 — Exploration Decision Engine

~~~text
bounded model loop
decision repair
repeated guard
final validation
~~~

### Commit 6 — Scoped Action Execution

~~~text
ActionMaterializer
input schema validation
checkpoint-before-dispatch
Invoker / Provider resume integration
~~~

### Commit 7 — Exploration Plan Node

~~~text
PlanNodeKind.EXPLORATION
Node spec / validator
Scheduler / node executor
nested state checkpoint
~~~

### Commit 8 — Explore WAITING / Restart

~~~text
approval action refs
async action refs
resume / crash recovery
SQLite restart tests
~~~

### Commit 9 — EXPLORE Handle Path

~~~text
ExplorationPlanFactory
Coordinator branch
availability / profile configuration
~~~

### Commit 10 — HYBRID

~~~text
hybrid-capable PlanDraft
profile materialization
Plan → Explore → downstream
~~~

### Commit 11 — Plan Patch / Revision

~~~text
PRE_PATCH
append-only validator
StateStore CAS
revision migration / audit
~~~

### Commit 12 — Agentic Observability

~~~text
Explore / Action / Patch trace
events
redaction gates
~~~

### Commit 13 — Stage 3C Acceptance / Docs

~~~text
tests/stage3c
full regression
README / design / ADR
~~~

---

## 16. 最终验收场景

### 场景 A：默认配置继续 fail-closed

~~~text
build_harness()
Request.mode = EXPLORE / HYBRID
~~~

验证：

~~~text
MODE_NOT_AVAILABLE
model calls = 0
planner calls = 0
capability calls = 0
StateStore records = 0
~~~

### 场景 B：deterministic-first

~~~text
AUTO + explicit target
AUTO + static input rule
fixed PLAN
fixed EXPLORE
fixed HYBRID
~~~

验证模型 Router calls 均为 0。

只有 AUTO / FAST 的 deterministic RouterNotApplicableError 才调用模型一次。
legacy RuleRouter(fallback=model) 经装配迁移后也不得出现内外两次 fallback。

### 场景 C：模型只填写未知字段

fixed FAST：

~~~text
model output = capability selection only
Harness materializes FAST RouteDecision
~~~

恶意输出：

~~~text
mode
route_type
source
provider_id
plugin_id
explorer_id
~~~

均被 Schema 拒绝，业务调用为 0。

### 场景 D：Strict Structured Output

验证：

~~~text
strict Provider selected
unsupported Provider ineligible
nested type mismatch rejected
enum mismatch rejected
additional property rejected
refusal rejected
truncated output rejected
no silent fallback
first Provider consumed tokens then failed + fallback succeeded -> both attempts accounted
refused / truncated / failed generation with complete accounting charges actual usage
failed generation with incomplete accounting remains ORPHANED at worst-case reservation
prepare_generation performs zero network calls
reservation covers every permitted slot with sound token/cost upper bounds
no receipt or slot STARTED CAS -> outbound=0
cancel/handoff wins before slot start -> stale receipt outbound=0
restart sees RESERVED/RUNNING -> ORPHANED; same generation/slot is never replayed
~~~

### 场景 E：Plan identity

同一个 Static template 连续 handle 两次：

~~~text
plan_id_1 != plan_id_2
revision_1 = revision_2 = 1
two StateStore records
~~~

模型注入 plan_id / revision / idempotency 失败。
自定义 Planner 连续返回同一 ExecutionPlan candidate 时，两次 handle 仍物化为不同
plan_id；execute_plan 则保持输入 identity 并在重复 create 时冲突。

### 场景 F：Multi-step EXPLORE success

~~~text
Request EXPLORE
  ↓
Action 1 READ
  ↓ Observation 1
Action 2 READ
  ↓ Observation 2
Finish
~~~

验证：

~~~text
fresh plan_id / exploration_id
each proposal checkpointed before call
every action through PRE_EXECUTE / Invoker
bounded evidence refs
final SUCCESS
single REQUEST trace
~~~

### 场景 G：Action scope / input / repeated guard

分别测试：

~~~text
unknown capability
MODEL capability
outside profile scope
WRITE when allow_write=false
external egress denied
input schema mismatch
oversized input
repeated same action
recursive exploration
~~~

均在业务调用前 fail-closed。

### 场景 H：Budget

分别耗尽：

~~~text
max_steps
max_model_calls
max_action_calls
max_total_tokens
deadline
max_observations
max_patch_count
max_exploration_depth
cost unavailable
~~~

验证 usage 不回退，resume 不重置；首 Provider 满额失败 + fallback 满额成功不会
在 generation 途中超过预留的 hard budget。无法提供 sound input token bound 或同 unit
finite cost upper bound 的 Provider 在任何网络调用前 ineligible。

### 场景 I：Approval WAITING / Restart

~~~text
Action proposal checkpoint
  ↓
PRE_EXECUTE REQUIRE_APPROVAL
  ↓
Plan WAITING
  ↓
SQLite restart
  ↓
resolve_approval
  ↓
resume same Action
~~~

验证：

~~~text
model decision calls unchanged
same action_id / proposal hash
same selected provider / policy decision hash
grant cannot authorize next Action
provider substitution / stale grant rejected
ApprovalDecision=REJECTED terminates exploration without another model turn
two workers resolve same approval -> exactly one CAS claim succeeds
crash after Grant CAS before PRE_EXECUTE -> exact persisted BoundApprovalState resumes
duplicate decision returns persisted outcome; same approval_id with different input hash fails
no duplicate WRITE
~~~

### 场景 J：Async WAITING / Restart

~~~text
Action returns ACCEPTED(job_ref)
  ↓
outer node WAITING
  ↓
SQLite restart
  ↓
complete_async_node
  ↓
Action result → Observation
  ↓
next model turn
~~~

验证 complete_async_node 不会提前终结 Exploration node，且 missing/mismatched
execution_ref、wrong/old job_ref、上一个 Action 的迟到 callback、duplicate/concurrent callback
均在 CAS 后保持 record 不变，不能写入当前 Action。

### 场景 K：Crash windows

注入：

~~~text
after model result before proposal checkpoint
after proposal checkpoint before Provider call
during Provider attempt
after Provider success before Observation checkpoint
after async terminal callback before Observation checkpoint
after Observation checkpoint before next turn
after model reservation before/after Provider generation
after external operation claim before scheduler handoff
after approval Grant CAS before PRE_EXECUTE / PRE_PATCH
after immutable transition delta CAS conflict / bounded reload
before/after atomic Exploration+outer-node terminal checkpoint
~~~

分别验证：

~~~text
READ safe recovery
idempotent WRITE safe recovery
non-idempotent ambiguous WRITE fail-closed
completed Action no duplicate
terminal Action with missing Observation is projected once without Provider/job replay
Exploration terminal and outer node never persist as split-brain statuses
orphaned model reservation is not refunded
claim lease takeover fences old owner and reuses durable operation input
each transition increments state_version exactly once; concurrent callbacks never merge snapshots
~~~

### 场景 L：HYBRID

不产生 Patch 的基础 HYBRID：

~~~text
n1 capability
  ↓
n2 exploration
  ↓
n3 downstream report
~~~

验证：

~~~text
Planner only once
Explore node result binding
WAITING propagation
restart
single plan_id
single trace tree
~~~

### 场景 M：PlanPatch accepted

~~~text
HYBRID exploration
  ↓
append-only Patch
  ↓
PRE_PATCH / PRE_PLAN
  ↓
CAS
  ↓
revision 1 → 2
  ↓
new nodes execute
~~~

验证：

~~~text
same plan_id
non-source existing states unchanged
source exploration / outer node only perform the allowed terminal transition
source outer node.result and Exploration.final_result are identical SUCCESS accepted envelope
matching patch pending indexes cleared; Plan WAITING -> RUNNING before handoff
new states PENDING
each new node has persisted PatchNodeOrigin
RevisionAudit exists
restart continues revision 2
ordinary revision-N checkpoint racing CAS cannot overwrite revision 2
accepted revision reloads through RecoveryValidator before new-tail admission
Patch absolute deadline is checked at admission and every retry/fallback outbound
Catalog descriptor drift after accept -> zero outbound
~~~

Patch REQUIRE_APPROVAL 额外验证：

~~~text
proposal checkpoint -> WAITING_APPROVAL -> SQLite restart
-> same patch_id / proposal_hash / policy hash
-> full PRE_PATCH + PlanValidator + PRE_PLAN revalidation
-> CAS once
-> persisted Grant consumed by exact Patch
~~~

mismatched / stale Grant 必须零 Plan mutation。

### 场景 N：PlanPatch rejected

分别测试：

~~~text
base revision conflict
replace existing node
remove edge
incoming edge to existing node
scope escalation
budget escalation
added CAPABILITY nodes exceed remaining action budget
node/edge/provider-attempt reservation exceeds persisted PatchExecutionLimits
Patch adds MODEL / EXPLORATION / WRITE / external-egress work
unknown capability
cycle
allow_patch exploration is not an execution-wide barrier
quiesce timeout / old in-flight callback
Patch absolute deadline already expired
accepted Patch origin missing or descriptor changed
Policy DENY
unsupported CAS Store
two concurrent patches
~~~

验证：

~~~text
at most one CAS succeeds
invalid Patch zero new node execution
Plan revision unchanged
technical rejection resumes only through a new scheduler generation
Policy/human rejection is governed terminal with no new model turn
governed rejection persists matching DENIED result/error/timestamps on child/outer/Plan
QUIESCE_TIMEOUT persists terminal FAILED truth and never starts a replacement Scheduler
~~~

### 场景 O：Cancellation / corrupted checkpoint

SQLite restart 后分别取消 WAITING Action 和 WAITING Patch，验证 Action/Patch/Exploration/
outer node/Plan 持久化收敛为 CANCELLED，迟到 Approval 或 job callback 不能恢复执行。

分别篡改 checkpoint 中的：

~~~text
exploration map key
pending action / patch ref
proposal hash / provider selection
outer/child WAITING continuation
observation source ref
revision audit chain
model reservation / slot state
approval Grant consumption / operation claim history
patch node origin / per-node ledger / deadline
applied transition id / fact hash
usage / state version
~~~

ExplorationRecoveryValidator 必须在任何新的模型或业务调用之前 fail-closed。
旧第三方 StateStore subclass 必须仍可实例化并执行 Direct / 普通 PLAN；
EXPLORE / HYBRID 则因 CAS_UNSUPPORTED 在任何模型/业务调用前 fail-closed。

### 场景 P：Regression

完整执行：

~~~text
Stage 1 Direct Invocation
Stage 2 Plan / Retry / Approval / Async / Resume
Stage 3A Provider Fabric
Stage 3B Routing & Planning
Stage 3C Agentic Exploration
~~~

invoke / execute_plan / resume_plan 旧 API 与旧 checkpoint 必须继续通过。

---

## 17. Stage 3C Acceptance Gate

新增目录：

~~~text
tests/stage3c/
  support.py
  test_identity_materialization.py
  test_structured_output.py
  test_routing_pipeline.py
  test_exploration_contracts.py
  test_exploration_success.py
  test_exploration_scope_budget.py
  test_model_generation_reservation.py
  test_exploration_waiting.py
  test_exploration_restart.py
  test_exploration_cancellation.py
  test_exploration_recovery_validation.py
  test_hybrid.py
  test_plan_patch.py
  test_plan_patch_approval_restart.py
  test_plan_patch_provenance_deadline.py
  test_execution_transition_cas.py
  test_observability.py
  test_regression_gate.py
~~~

Gate 必须使用：

~~~text
deterministic local model providers
deterministic local business capabilities
InMemory / SQLite StateStore
Fault injection
No network
No real vendor SDK
~~~

最低断言：

1. 非法 Proposal 零业务执行。
2. 静态 Route 命中零模型调用。
3. Action 执行前必有 checkpoint。
4. 所有真实动作经过 CapabilityInvoker。
5. WRITE 不放宽 3A 安全规则。
6. WAITING / Resume 不重复稳定决策或动作。
7. Patch 同 plan_id、revision 精确加一。
8. Patch 不能改历史。
9. CAS 冲突 fail-closed。
10. 模型 generation 无 reservation/slot fencing 零 outbound，crash reservation 只 ORPHANED。
11. External operation claim / typed Grant 可恢复且旧 owner 被 fencing。
12. 每条 CAS transition 由 Coordinator 唯一 version+1。
13. Patch accepted/rejected 的 child/outer/Plan terminal payload 完整一致。
14. Patch-added node 的 origin / ledger / deadline 每次 dispatch 都重验。
15. Trace / Events 不泄漏敏感内容。
16. 所有历史 Gate 通过。

建议命令：

~~~bash
.venv/bin/python -m pytest tests/stage3c -v
~~~

完整回归命令在实现时同步根 README 与 tests/stage3c/README，必须包含所有 workspace 模块和
tests/stage2、tests/stage3a、tests/stage3b、tests/stage3c。

---

## 18. 3C 完成定义

以下条件全部满足后，Stage 3C 才能标记完成：

- plan_id fresh execution identity 已冻结并由 Materializer 统一生成；
- StaticPlanner 模板重复执行不会复用 plan_id；
- RoutingPipeline deterministic-first 成为默认安全装配；
- 只有 typed NOT_APPLICABLE 进入模型 fallback；
- LLM Router Prompt 不包含 requested/effective mode；
- 模型只生成最小 Route Draft，Harness 生成 RouteDecision；
- Strict Structured Output 支持 Provider feature 协商；
- REQUIRED 不静默降级；
- 完整本地 Schema + Pydantic + semantic validation 生效；
- 模型调用使用 prepare → reservation CAS → per-slot fencing，crash 不重放同一 generation；
- EXPLORE / HYBRID 在正确配置时可执行；
- 未配置 Explorer 时继续 fail-closed；
- ExplorationEngine 由 Harness 持有；
- 模型 / Plugin 不持有 Invoker / Provider / StateStore；
- 每个 Action 有显式有限 scope；
- 每个 Action proposal-before-dispatch；
- Action input schema 校验真实生效；
- steps / model calls / action calls / tokens / deadline / repeat / patch / depth 有上限；
- cost accounting 不可用时不伪造 0 成本；
- WRITE Action 遵守 idempotency / equivalence / Provider resume；
- standalone EXPLORE 是真实单 EXPLORATION 节点 Plan；
- HYBRID 使用显式 EXPLORATION node；
- Approval / Async WAITING 可跨 SQLite restart；
- typed Grant、ExternalOperationClaim 与 takeover fencing 可跨 crash 恢复；
- complete_async_node 不误完成整个探索节点；
- PlanPatch v1 append-only；
- PRE_PATCH / PRE_PLAN / PlanValidator 均生效；
- Patch 使用 CAS，revision 单调、plan_id 不变；
- Coordinator 独占 CAS transition / state_version，Scheduler 不提交预改 whole-record snapshot；
- Patch source 是 execution-wide barrier；新增节点始终受 persisted origin / ledger / absolute
  deadline 约束；
- RevisionAudit 进入 checkpoint；
- 不持久化 raw Prompt / raw response / hidden CoT；
- 不引入 WorkflowSPI / Catalog / 自动发布；
- Stage 1 / 2 / 3A / 3B / 3C 全量 Gate 通过。

---

## 19. 已确认的 Stage 3C ADR

### ADR-P3C-001：Application API 分层

**决议**：

~~~text
handle = standard orchestration API
invoke / execute_plan = stable advanced APIs
~~~

不删除低层 API。

### ADR-P3C-002：Routing deterministic-first

**决议**：

~~~text
primary deterministic Router
only NOT_APPLICABLE
fallback model Router
~~~

这是默认安全装配不变量。

### ADR-P3C-003：模型只生成未知字段

**决议**：

模型生成 Route / Plan / Action / Patch Draft；Harness materialize final identities、
mode、route type、scope、budget、idempotency 与 revision。

### ADR-P3C-004：Strict Structured Output

**决议**：

Provider-native strict 优先；unsupported 不静默降级；完整本地 Schema 与业务 Validator 永久保留。

### ADR-P3C-005：Plan identity

**决议**：

plan_id 是 fresh execution lineage identity；template / workflow identity 分离；Patch 只增加 revision。

### ADR-P3C-006：Harness-owned Exploration

**决议**：

模型无执行权；ScopedActionExecutor 是唯一 Action → CapabilityInvoker 桥。

### ADR-P3C-007：Standalone EXPLORE 使用真实 Plan

**决议**：

standalone EXPLORE materialize 为单 EXPLORATION 节点 ExecutionPlan；Exploration 子状态嵌入同一
PlanExecutionRecord，避免第二套 execution truth。

### ADR-P3C-008：Checkpoint-before-dispatch

**决议**：

ActionProposal 必须先 checkpoint 再执行；恢复同一 Action，不重新模型决策。

### ADR-P3C-009：Action-level WAITING

**决议**：

Continuation / Approval 使用 action-level ExecutionUnitRef；外层 Plan API 继续兼容。

### ADR-P3C-010：HYBRID 是显式 Plan node

**决议**：

HYBRID 用 PlanNodeKind.EXPLORATION；不把 ExplorationEngine 注册成业务 Capability。

### ADR-P3C-011：PlanPatch append-only + CAS

**决议**：

v1 Patch 不修改既有节点/边/历史；同 plan_id、revision+1；PRE_PATCH / PRE_PLAN /
PlanValidator / StateStore CAS 全部成功后才可见。

### ADR-P3C-012：No hidden CoT

**决议**：

只保存结构化决策事实、hash、Observation 摘要和 evidence refs，不保存隐藏推理。

### ADR-P3C-013：Workflow 自动晋升延后

**决议**：

3C 只记录安全 candidate facts；3D Eval；Stage 4 版本化发布。

### ADR-P3C-014：不新增 ModelConnector SPI

**决议**：

ModelProvider 继续是唯一模型 Provider 边界；通用严格生成助手命名为
StructuredGenerationAdapter，具体厂商连接逻辑属于 Provider 实现。

---

## 20. 文档同步要求

3C 实现完成时至少更新：

~~~text
README.md
harness-contracts/README.md
harness-model/README.md
harness-routing/README.md
harness-planning/README.md
harness-agentic/README.md
harness-policy/README.md
harness-execution/README.md
harness-state/README.md
harness-bootstrap/README.md
harness-trace/README.md
harness-events/README.md
tests/README.md
tests/stage3c/README.md
FinanceClaw 第三阶段说明书
Stage 3 ADR 状态摘要
~~~

Stage 3B 实施说明书继续作为“3B 当时完成的实现基线”，不回写成已经实现 3C。

`tests/stage3b/README` 已初步明确下列边界；3C 实施和后续文档必须持续保持：

~~~text
EXPLORE / HYBRID / PlanPatch → Stage 3C
Replay Eval                  → Stage 3D
~~~

---

## 21. 一句话原则

> **Stage 3C 不是让模型获得执行权，而是让模型在一个由 Harness 限定、落盘、验证和恢复的循环里，只提出下一步未知决策。**

实施口诀：

~~~text
Deterministic First
      ↓
Model Fills Unknowns
      ↓
Validate
      ↓
Checkpoint
      ↓
Execute Through Harness
      ↓
Observe
      ↓
Patch Only by Governance
~~~
