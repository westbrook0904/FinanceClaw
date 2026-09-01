# FinanceClaw Agent Foundation 一期实施说明书

> **文档性质**：当前实施基线
> **版本**：V1.3（Foundation F5 Gate 就绪状态）
> **日期**：2026-09-01
> **优先级**：低于已冻结的 Stage 1 / 2 / 3A / 3B 运行契约，高于旧 Stage 3C 高阶草案
> **路线图**：`FinanceClaw-Agent-Foundation-一期路线图.md`

---

## 1. 一期交付目标

一期只交付一个可真实使用、可观察、可继续演进的单 Agent 基础闭环：

```text
Request
  ↓
Context Assembly / Projection
  ↓
FAST / PLAN / standalone EXPLORE
  ↓
Structured Decision
  ↓
Scope / Policy / Schema Validation
  ↓
CapabilityInvoker
  ↓
Observation / Result
  ↓
Governed Memory Read / Write
```

实现顺序固定为：

```text
Routing correctness
  → Context Engineering
  → Memory Foundation
  → Minimal Explore
  → Real-use Gate
```

一期不以模式数量、动态 DAG 或复杂恢复作为完成度指标。

## 2. 四种执行模式的当前边界

公开 Contract 还包含 `AUTO`；它只是请求级模式选择入口，Router 完成后必须收敛为下表某一最终
执行路径，因此不算第五种执行器。

| 模式 | 一期语义 | 当前状态 |
|---|---|---|
| FAST | Router 确定一个 Capability，直接进入受治理调用链 | 已有基线，继续使用 |
| PLAN | Planner 在执行前生成完整 DAG，ExecutionEngine 执行一次 | 已有基线，继续使用 |
| EXPLORE | 外层是一个单 `EXPLORATION` 节点 Plan；节点内部每轮最多调用一个 Capability | F4b 已实现，需显式单写者配置 |
| HYBRID | 稳定宏观 Plan 中嵌入探索节点 | 保留枚举，继续 fail-closed |

### 2.1 EXPLORE 的准确执行方式

standalone EXPLORE 在请求入口只创建一次 Plan：

```text
EXPLORE RouteDecision
  ↓
ExplorationPlanFactory
  ↓
ExecutionPlan(fresh plan_id, one EXPLORATION node)
  ↓
ExecutionEngine
  ↓
ExplorationEngine
```

`ExplorationEngine` 每轮根据 Context、Memory 和已有 Observation 生成一个严格结构化决策：

```text
call_one_capability | finish
```

选择 `call_one_capability` 时，动作直接经过：

```text
ScopedActionExecutor → CapabilityInvoker
```

它不重新进入 `handle()`，不复用 FAST Router，也不为每一轮生成新的 Plan。FAST 是请求级路由
模式；Explore Action 是已在可信 scope 内物化的内部执行单元，两者只复用底层
`CapabilityInvoker`。

### 2.2 HYBRID 的后续设计假设

HYBRID 本期不实现。若一期真实使用证明存在需求，优先评估的形态是“一次生成并执行一个稳定
宏观 Plan，其中包含显式 `EXPLORATION` 节点”：

```text
stable Plan
  ├── known capability node
  ├── exploration node
  │     └── internal one-action-per-turn loop
  └── known capability node
```

它不是每轮重新规划整份 Plan。探索结果是否需要修改主 Plan、是否需要 PlanPatch，必须由真实
失败案例触发独立 ADR；不能把 PlanPatch 预设为 HYBRID 的必备机制。

## 3. 一期模块边界

### 3.1 `harness-context`

负责：

- 从受信任来源收集 ContextItem；
- 规范化、去重、排序和来源校验；
- 按用途执行 Policy 与确定性裁剪；
- 为 Router、Planner 和 Explorer 生成不同最小投影；
- 记录 projection hash、被省略项和原因。

不负责：

- 执行业务 Capability；
- 保存执行状态；
- 决定 Memory 的持久化；
- 通过 Prompt 文本替代 Policy enforcement。

### 3.2 `harness-memory`

负责：

- 隔离和检索跨请求的显式长期事实；
- 校验并执行受治理的写入、删除和过期；
- 向 ContextAssembler 返回有界 MemorySlice；
- 提供 InMemory 和 SQLite 两种基础实现。

不负责：

- 保存 Plan、Node、Action 的运行状态；
- 自动相信模型输出；
- 保存隐藏推理、Secret、原始 Prompt 或完整 Provider response；
- 在一期引入向量数据库、自动压缩或自主长期记忆。

### 3.3 `harness-agentic`

负责最小串行 Exploration 状态机、结构化 turn、Action materialization、Observation 投影和基础
次数限制。它只能通过 `ScopedActionExecutor` 使用现有 `CapabilityInvoker`，不能得到 Registry、
Provider、Plugin 或 StateStore 的通用 service locator。

### 3.4 既有模块

- `harness-routing`：保持 deterministic-first，并消费 route projection；
- `harness-planning`：消费 plan projection，不直接查询 MemoryProvider；
- `harness-execution`：执行真实 Plan 与 `EXPLORATION` 节点；
- `harness-state`：仍是当前执行真相；
- `harness-policy`：约束 Context、Memory 和 Action，不能被 Prompt 绕过；
- `harness-model`：提供 strict structured generation，不承担业务编排。

## 4. Context Engineering

### 4.1 Context 与其他数据的边界

```text
InvocationContext  = 请求身份、租户、deadline、trace 等调用事实
ExecutionState     = 当前 Plan / Node / Action 执行到哪里
Memory             = 经过治理、允许跨请求复用的长期事实
ContextSnapshot    = 某次模型决策可使用的、带来源的不可变输入集合
ContextProjection  = 面向某个消费者的最小 Prompt 输入
Secret             = 永不作为普通 ContextItem 进入模型
```

ContextSnapshot 可以引用 Memory 和 Observation，但不能成为它们新的真相来源。

### 4.2 最小 Contracts

```python
class ContextSourceRef(ContractModel):
    source_kind: Literal[
        "system_instruction",
        "request",
        "session",
        "memory",
        "capability_catalog",
        "observation",
    ]
    source_id: NonEmptyString

class ContextItem(ContractModel):
    item_id: NonEmptyString
    kind: NonEmptyString
    content: FrozenJsonValue
    source: ContextSourceRef
    provenance: ContextProvenance
    freshness: ContextFreshness
    trust_tier: ContextTrustTier
    sensitivity: ContextSensitivity
    created_at: datetime
    expires_at: datetime | None = None

class ContextSnapshot(ContractModel):
    snapshot_id: NonEmptyString
    items: tuple[ContextItem, ...]
    canonical_hash: NonEmptyString
    created_at: datetime

class ContextProjection(ContractModel):
    consumer: Literal["route", "plan", "explore"]
    snapshot_id: NonEmptyString
    items: tuple[ContextItem, ...]
    omitted: tuple[ContextOmission, ...]
    projection_hash: NonEmptyString

class ContextUseRecord(ContractModel):
    use_id: NonEmptyString
    consumer: Literal["route", "plan", "explore"]
    snapshot_id: NonEmptyString
    snapshot_hash: NonEmptyString
    projection_hash: NonEmptyString
    included_item_ids: tuple[NonEmptyString, ...]
    omitted: tuple[ContextOmission, ...]
    assembled_at: datetime
```

`ContextOmission` 只记录 item_id 和固定 reason code，不复制被裁剪的敏感内容。

### 4.3 Snapshot 生命周期与稳定 Hash

每次 Router、Planner、Explorer decision 前先收集进程内 candidate items；完成规范化和
ContextPolicy 后，才物化一次 ContextSnapshot，并从该 Snapshot 生成对应 Projection。Policy 前的
candidate set 不进入 StateStore / Trace。Explore 将本轮 `ContextUseRecord` 与
`model_calls + 1` 在模型 outbound 前一起 checkpoint；记录数量天然受 `max_model_calls` 限制。

稳定性规则：

- `ContextItem.item_id` 由稳定 source identity + source version 派生，不能在 assemble 时随机生成；
- `canonical_hash` 只计算规范化、排序后的 item 事实；排除 snapshot_id、assembled/created time、
  trace ID 等运行身份；
- `projection_hash` 计算 consumer、included item facts 与 omission reason；排除随机 use/snapshot ID；
- 同一规范化输入必须产生相同 snapshot/projection hash，不要求产生相同运行 ID；
- ContextSnapshot / Projection 的原始敏感内容默认不写 StateStore 或 Trace，只持久化有界
  `ContextUseRecord`。需要审计原文时必须使用独立受控存储与 Policy，不在一期默认实现。

### 4.4 Pipeline

```text
ContextSource.collect()
  ↓
ContextAssembler.normalize / deduplicate / order（transient candidates）
  ↓
ContextPolicy.filter
  ↓
ContextAssembler.materialize_snapshot
  ↓
ContextProjector.project(consumer)
  ↓
PromptBuilder
```

Router、Planner、Explorer 不得自行拼接 Memory、Capability Catalog 或 Invocation baggage；它们只
接收对应的 `ContextProjection`。

### 4.5 信任与优先级

固定规则：

1. Harness system instruction 与受信任应用配置是指令；
2. 用户请求只在授权范围内表达任务意图；
3. Memory、Capability output、Observation 和外部内容一律作为数据；
4. 数据中的文本不能把自身提升为 system instruction；
5. Policy 在 Prompt 外执行，任何 Context 文本都不能覆盖它。

相同优先级使用稳定 source order 和 item_id 排序，确保同一输入得到相同 projection hash。

### 4.6 一期限制

一期只使用确定性限制：

```text
max_items
max_chars
max_chars_per_item
max_observations
max_memory_records
```

不引入 tokenizer、token budget、成本预算或模型相关压缩器。超限时按固定优先级裁剪，并记录
omission reason。

## 5. Memory Foundation

### 5.1 最小 Contracts

```python
class MemorySubjectScope(ContractModel):
    tenant_id: NonEmptyString
    subject_id: NonEmptyString

class MemoryRecord(ContractModel):
    memory_id: NonEmptyString
    tenant_id: NonEmptyString
    subject_id: NonEmptyString
    namespace: NonEmptyString
    kind: Literal["conversation", "preference", "domain_fact"]
    content: FrozenJsonValue
    tags: frozenset[NonEmptyString] = Field(max_length=16)
    sensitivity: MemorySensitivity
    provenance: MemoryProvenance
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None

class MemoryQuery(ContractModel):
    tenant_id: NonEmptyString
    subject_id: NonEmptyString
    namespaces: frozenset[NonEmptyString] = Field(min_length=1, max_length=8)
    kinds: frozenset[MemoryKind] = Field(min_length=1, max_length=3)
    tags: frozenset[NonEmptyString] = Field(max_length=16)
    text: Annotated[str, Field(min_length=1, max_length=512)] | None
    limit: int = Field(default=20, ge=1, le=50)

class MemorySlice(ContractModel):
    records: tuple[MemoryRecord, ...] = Field(max_length=50)
    query_hash: NonEmptyString
    truncated: bool

class MemoryWriteDraft(ContractModel):
    kind: MemoryKind
    content: FrozenJsonValue
    tags: frozenset[NonEmptyString] = Field(max_length=16)
    evidence_refs: tuple[NonEmptyString, ...] = Field(min_length=1, max_length=32)

class MemoryWriteProposal(ContractModel):
    proposal_id: NonEmptyString
    proposal_hash: NonEmptyString
    tenant_id: NonEmptyString
    subject_id: NonEmptyString
    namespace: NonEmptyString
    kind: MemoryKind
    content: FrozenJsonValue
    tags: frozenset[NonEmptyString] = Field(max_length=16)
    sensitivity: MemorySensitivity
    evidence_refs: tuple[NonEmptyString, ...] = Field(min_length=1, max_length=32)
    source_fact_hash: NonEmptyString
    provenance: MemoryProvenance
    expires_at: datetime | None
```

`MemoryWriteDraft` 是模型最多可以提供的内容提议，不包含身份、namespace、sensitivity、retention
或持久化 ID。`MemoryWriteProposal` 由 Harness 使用受信任 InvocationContext、应用配置、分类器/
确定性规则和已完成结果物化；模型输出的同名字段必须被 Schema 拒绝。evidence_refs 必须非空，
并能解析到当前受信任 Request item、Observation 或 completed Result；任意字符串引用不成立。

Gateway 还必须按 canonical JSON 字节数执行硬限制：单条 MemoryRecord / WriteProposal 默认
不超过 32 KiB，单次 MemorySlice 默认不超过 128 KiB；配置只能收紧。Provider 返回超量或超过
query.limit 的记录时，Gateway 先稳定裁剪并标记 `truncated=true`，不能把无界结果交给 Context。

### 5.2 SPI 与 Gateway

```text
MemoryProvider
  get(memory_id)
  search(query)
  put_if_absent(record, proposal_hash)
  delete(memory_id)

MemoryGateway
  get(trusted_scope, namespace, memory_id)
  search(trusted_scope, query)
  put(trusted_scope, proposal)
  delete(trusted_scope, namespace, memory_id)
  validate namespace / tenant / subject on every operation
  apply MemoryPolicy
  bound query and result size
  materialize identity and provenance
  call configured provider
```

业务层、Router、Planner 和 Explorer 只依赖 `MemoryGateway` 或 Context projection，不直接选择
MemoryProvider。Provider 的 ID-only 方法是存储内部 SPI，不是公开授权边界。

### 5.3 读取流程

```text
trusted request identity
  ↓
bounded MemoryQuery
  ↓
MemoryPolicy
  ↓
MemoryGateway.search
  ↓
MemorySlice
  ↓
ContextAssembler
```

必须校验 tenant、subject 和 namespace，结果必须带 provenance、freshness/expiry，并按稳定规则
排序和裁剪。

Gateway 的 get/delete 必须先按 ID 加载，再验证 record 的 tenant、subject、namespace 与受信任
scope 完全一致，并重新执行 read/delete Policy；不得因调用方知道 memory_id 就跨 scope 访问。

### 5.4 写入流程

```text
explicit application request or completed structured result
  ↓
optional MemoryWriteDraft
  ↓
Harness binds tenant / subject / namespace / sensitivity / retention
  ↓
MemoryWriteProposal
  ↓
schema / evidence / sensitivity validation
  ↓
MemoryPolicy
  ↓
MemoryGateway materializes MemoryRecord
  ↓
MemoryProvider.put
```

模型最多提出不带身份与存储控制字段的 Draft，不能直接构造可信 Proposal 或写库。没有 evidence
的推断、执行中间态、Secret 和 hidden chain-of-thought 必须拒绝。Memory 写入失败不回滚已经
成功的业务 Action，只在结果和 Trace 中报告独立 issue。

### 5.5 Provider 顺序

1. `InMemoryMemoryProvider`：契约测试和本地开发；
2. `SQLiteMemoryProvider`：一期真实试用的持久化基线；
3. 只有确定性 filter / tag / text search；
4. 向量检索和自动 compact 等真实检索质量不足后再立项。

一期 `put` 采用 create-only 语义：proposal_id + canonical proposal hash 相同的重复写返回同一
MemoryRecord；同 proposal_id 不同 hash 稳定失败。`updated_at == created_at`，更新/合并接口暂不
提供；需要改变事实时显式 delete 后创建新记录。delete 是 scope-checked、幂等操作。

### 5.6 与统一 PolicyEngine 的关系

ContextPolicy / MemoryPolicy 不是新的自由 bool 回调，也不各自创建 Policy SPI。它们复用现有
`harness-policy` 的类型化链，并新增最小 phase：

```text
PRE_CONTEXT
PRE_MEMORY_READ
PRE_MEMORY_WRITE
PRE_MEMORY_DELETE
```

基础 redaction、trust、scope 和 evidence guard 始终执行；Policy 只能进一步收紧。上述 phase
一期只接受 ALLOW / DENY，`REQUIRE_APPROVAL` 视为不支持并 fail-closed。Action 继续使用现有
PRE_EXECUTE。

## 6. Minimal Explore

### 6.1 Profile 与基础次数限制

```python
class CapabilityCompletionMode(StrEnum):
    UNKNOWN = "unknown"
    SYNC = "sync"
    ASYNC = "async"

# Foundation 对既有 CapabilityExecutionProfile 的兼容扩展：
# completion_mode: CapabilityCompletionMode = CapabilityCompletionMode.UNKNOWN

class ExplorationBudget(ContractModel):
    max_steps: int
    max_model_calls: int
    max_action_calls: int
    max_repeated_actions: int
    max_observations: int

class ExplorationUsage(MutableContractModel):
    steps: int
    model_calls: int
    action_calls: int

class ExplorationProfile(ContractModel):
    profile_id: NonEmptyString
    model_capability_id: NonEmptyString
    allowed_capability_ids: frozenset[NonEmptyString]
    default_budget: ExplorationBudget
    prompt_version: NonEmptyString
    memory_required: bool = False

class ExplorationProfileSnapshot(ContractModel):
    profile_id: NonEmptyString
    model_capability_id: NonEmptyString
    allowed_capability_ids: frozenset[NonEmptyString]
    budget: ExplorationBudget
    prompt_version: NonEmptyString
    memory_required: bool
    profile_hash: NonEmptyString

class ExplorationNodeSpec(ContractModel):
    exploration_id: NonEmptyString
    goal_bindings: FrozenBindingMapping
    profile: ExplorationProfileSnapshot

# Foundation 对既有 PlanNode 的 typed 扩展：
# exploration: ExplorationNodeSpec | None = None
```

约束：

- allowed capabilities 必须显式非空；
- MODEL、EXPLORATION 和内部控制 Capability 不能进入 Action scope；
- 一期固定 `side_effect ∈ {NONE, READ}`、`egress ∈ {NONE, INTERNAL}`；
- CapabilityExecutionProfile 必须显式声明 `completion_mode=SYNC` 才能进入 Explore scope；旧
  descriptor 缺少该字段时按 UNKNOWN 处理，仅从 Explore 排除，不影响既有 FAST / PLAN；
- nested exploration 固定禁止，不需要可配置 depth budget；
- 不包含 duration、token、cost、patch count 或 provider cost rate。

`ExplorationPlanFactory` 只创建 Harness-owned wrapper：恰好一个 EXPLORATION node、零 edge、Plan
output 只绑定该节点结果。该 node 必须有 `ExplorationNodeSpec`，不得同时出现 capability/approval
字段，Scheduler-level `max_attempts=1`。PlanValidator 对其他 node kind 反向禁止 exploration spec。
完整 `ExplorationProfileSnapshot` 随 Plan 首次 checkpoint，因此即使 State 尚未创建就重启，也能
从 Plan 中恢复同一可信 profile；不得放进自由 metadata。

基础计数采用简单的 write-ahead 规则：每次逻辑 generation（含 repair）在调用模型前增加
`model_calls`；`call_capability` 的 `steps + 1 / action_calls + 1` 与 ActionProposal 同一 checkpoint；
`finish` 的 `steps + 1` 与 terminal result 同一 checkpoint。finish 也算一步，已经增加的计数不因
失败或 restart 返还。

`max_observations` 直接约束 `len(observations)`，不复制 Usage 字段。repeated-action fingerprint
固定为 `sha256(capability_id + canonical_json(input))`，重复数由持久化 ActionProposal 推导，
`max_repeated_actions` 表示首次之后允许的重复次数；resume 不重置任何计数。

### 6.2 Turn Contract

```python
class CallCapabilityDraft(ContractModel):
    kind: Literal["call_capability"]
    capability_id: NonEmptyString
    input: RequestInput
    reason_code: NonEmptyString

class FinishDraft(ContractModel):
    kind: Literal["finish"]
    output: ResultOutput
    evidence_refs: tuple[NonEmptyString, ...]
    reason_code: NonEmptyString

ExplorationTurnDraft = Annotated[
    CallCapabilityDraft | FinishDraft,
    Field(discriminator="kind"),
]
```

模型不能输出 plan_id、node_id、exploration_id、action_id、status、budget、provider_id、plugin_id、
idempotency_key 或任何 Patch 字段。

### 6.3 最小持久化状态

```python
class ExplorationStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"

class ActionProposal(ContractModel):
    action_id: NonEmptyString
    exploration_id: NonEmptyString
    step: int
    capability_id: NonEmptyString
    input: RequestInput
    proposal_hash: NonEmptyString
    catalog_snapshot_hash: NonEmptyString
    scope_hash: NonEmptyString
    context_projection_hash: NonEmptyString
    reason_code: NonEmptyString

class ActionExecutionState(MutableContractModel):
    action_id: NonEmptyString
    status: Literal[
        "proposed", "running", "succeeded", "failed", "denied", "cancelled", "orphaned"
    ]
    proposal: ActionProposal
    result: ResultEnvelope | None
    error_code: NonEmptyString | None
    observation_id: NonEmptyString | None
    started_at: datetime | None
    completed_at: datetime | None

class Observation(ContractModel):
    observation_id: NonEmptyString
    action_id: NonEmptyString
    result_status: ResultStatus
    bounded_summary: FrozenJsonValue
    evidence_refs: tuple[NonEmptyString, ...]
    result_hash: NonEmptyString

class ExplorationState(MutableContractModel):
    exploration_id: NonEmptyString
    plan_id: NonEmptyString
    node_id: NonEmptyString
    profile: ExplorationProfileSnapshot
    status: ExplorationStatus
    usage: ExplorationUsage
    scope_hash: NonEmptyString
    context_uses: list[ContextUseRecord]
    actions: list[ActionExecutionState]
    observations: list[Observation]
    pending_action_id: NonEmptyString | None
    final_result: ResultEnvelope | None
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None
```

`ActionProposal` 的 identity、hash 与可信 scope/context 引用全部由 Harness 物化；模型只提供
`CallCapabilityDraft` 中的字段。`PlanExecutionState` 一期只新增
`explorations: dict[node_id, ExplorationState]`，不新增 Plan revision 或外部 operation ledger。
`ExplorationState.profile` 必须与外层 `ExplorationNodeSpec.profile` 字节级等价；budget guard 和
scope 只读取这份 persisted snapshot，不在 resume 时重新放宽。
除 `status=orphaned` 保存 unexpected ACCEPTED 事实外，Action terminal status 必须对应 terminal
ResultEnvelope；orphaned Action 不得生成 Observation 或恢复 callback。

不加入 Patch、Approval、Async、operation claim、lease、scheduler generation、revision history 或
跨 worker fencing 字段。

### 6.4 外层 Node 与 Exploration 原子一致性

外层 NodeExecutionState 与内层 ExplorationState 不是两份独立真相。每次创建、终结和恢复校验都
在同一个 PlanExecutionRecord 上完成：

```text
Exploration CREATED/RUNNING → outer node RUNNING
Exploration SUCCEEDED       → outer node SUCCEEDED + identical SUCCESS ResultEnvelope
Exploration PARTIAL         → outer node SUCCEEDED + identical PARTIAL ResultEnvelope
Exploration FAILED          → outer node FAILED    + identical FAILED ResultEnvelope
Exploration DENIED          → outer node DENIED    + identical DENIED ResultEnvelope
Exploration CANCELLED       → outer node CANCELLED + identical CANCELLED ResultEnvelope
```

`ExplorationState.final_result` 与 `NodeExecutionState.result` 必须是同一序列化 Envelope，hash
一致。finish 时 inner state、outer node、必要的 Plan status/issues、completed_at 与 state_version
在一次 StateStore save 中更新。resume 发现 status/result/profile 任一不一致时返回
`HARNESS.EXPLORATION.CHECKPOINT_CORRUPT`，不得猜测或自动修复。

首次启动必须原子写入 outer node RUNNING + 新 ExplorationState RUNNING，然后才能调用模型。仅当
outer node 仍为 PENDING/READY 时，缺少 ExplorationState 才表示尚未启动并可安全创建；outer node
已经 RUNNING/terminal 却缺少 child state 一律视为 checkpoint corruption。

### 6.4.1 一期 single-writer 前提

上述“一次 StateStore save”只承诺单 Harness 进程内的原子快照，不等同于跨 worker CAS。
一期 standalone EXPLORE 的部署与恢复契约明确限制为：

```text
one active ExecutionEngine owner per plan_id
one writer process for the configured Foundation StateStore
per-plan in-process lock serializes run / resume / cancel
resume only when the local active-owner registry has no owner
```

Composition Root 必须显式配置 `single_writer_guaranteed=true` 才能开放 EXPLORE；共享 SQLite、多个
worker、远程 callback 或无法证明单 owner 的部署返回 `MODE_NOT_AVAILABLE` / `RESUME_UNSAFE`，不能
依靠 load + save 冒充 exactly-once。进程崩溃后的恢复由唯一 replacement process 执行，运维层必须
先确保旧进程已终止；一期不支持并发 takeover。

同一进程内，Action proposal checkpoint、Provider outbound 与 terminal Observation checkpoint
仍遵守严格顺序；single-writer 只限定并发故障模型，不放宽 checkpoint-before-dispatch。需要共享
Store 或多 worker 时，必须重新启用 versioned CAS / fencing ADR 后才能开放该部署。

### 6.5 主循环

```text
load/create ExplorationState + outer node invariant
  ↓
collect candidates → ContextPolicy → ContextSnapshot / explore projection
  ↓
check basic count limits
  ↓
checkpoint ContextUseRecord + model_calls + 1
  ↓
strict structured generation
  ├── call_capability
  │     ↓
  │   materialize and validate ActionProposal
  │     ↓
  │   checkpoint PROPOSED
  │     ↓
  │   ScopedActionExecutor → CapabilityInvoker
  │     ↓
  │   checkpoint terminal Action + Observation
  │     ↓
  │   next turn
  │
  └── finish
        ↓
      validate evidence refs and output
        ↓
      checkpoint terminal result
```

每轮只允许一个分支和最多一个 Action。非法结构允许有限 repair；repair 只增加 model call count，
不创建 Action。模型 generation 期间崩溃不会产生业务副作用；已计数的调用不返还，恢复时只在
剩余次数允许的情况下从上一 completed Observation 生成新的 decision。

次数耗尽或 repeated-action guard 命中时：已有至少一个可靠 SUCCESS/PARTIAL Observation 则返回
PARTIAL + evidence refs；否则返回 FAILED。FinishDraft 的 evidence ref 无法解析时先走有限 repair，
repair 耗尽后返回 FAILED，不能把无证据输出降级为成功。

### 6.6 Action 安全顺序

```text
draft schema
  → capability scope and type
  → input schema
  → side_effect NONE/READ + egress NONE/INTERNAL + completion SYNC
  → repeated-action guard
  → count limits
  → proposal checkpoint
  → CapabilityInvoker provider resolution
  → PRE_EXECUTE Policy
  → Provider outbound
```

`CapabilityInvoker` 继续负责 Provider selection、PRE_EXECUTE、retry/fallback 和已有 timeout/deadline。
WRITE、EXTERNAL egress、ASYNC/UNKNOWN completion 在 dispatch 前即不具备 Explore eligibility。
PRE_EXECUTE 要求 Approval 时零 Provider outbound，以 `APPROVAL_UNSUPPORTED` governed terminal
结束 Exploration，不创建可恢复 WAITING。

若声明 SYNC 的 Provider 违反契约并在 outbound 后返回 `ACCEPTED + job_ref`，该调用已发生，不能
描述成普通“安全拒绝”。Harness 必须禁止 retry/fallback，把 exact ACCEPTED ResultEnvelope 写入
Action、将 Action 标记 ORPHANED、将 Exploration/outer node 标记 FAILED，并记录
`HARNESS.EXPLORATION.ASYNC_CONTRACT_VIOLATION`。一期不接收其 completion callback，也不声称已撤销
外部 job。

Action outcome 固定映射：

| Result / Guard | Action / Exploration | 是否形成 Observation 后继续 |
|---|---|---:|
| malformed Draft | 不创建 Action；有限 repair | 否 |
| scope/type/sync invariant violation | 不创建 Action / DENIED | 否，零 outbound |
| PRE_EXECUTE Policy deny/approval | DENIED / DENIED | 否，零 outbound |
| SUCCESS / PARTIAL | SUCCEEDED / RUNNING | 是 |
| 已通过治理后的普通 FAILED | FAILED / RUNNING | 是，摘要必须有界 |
| CANCELLED / deadline | CANCELLED 或 FAILED / 同步终止 | 否 |
| unexpected ACCEPTED + job_ref | ORPHANED / FAILED | 否 |

### 6.7 恢复边界

一期只支持从“最后一个 Observation 已完整 checkpoint，且 `pending_action_id=None`”的边界继续。

```text
completed Observation boundary → 可以继续下一 turn
PROPOSED / RUNNING Action       → RESUME_UNSAFE
Approval / Async waiting        → 不创建该状态，fail-closed
unexpected provider ACCEPTED    → ORPHANED + FAILED，不接 callback
cross-worker callback/takeover  → 不支持
```

Action terminal result、Observation 和清除 `pending_action_id` 应作为同一个 PlanExecutionRecord
checkpoint 保存。若无法确认 outbound 是否发生，不重放 Action，也不重新询问模型替换它。

## 7. Context、Memory 与执行状态的组合

```text
StateStore
  └── 保存当前 plan/node/exploration/action/observation 执行事实

MemoryProvider
  └── 保存获准跨请求复用的 conversation/preference/domain fact

ContextAssembler
  ├── 读取受信任 request/session facts
  ├── 经 MemoryGateway 获取 MemorySlice
  ├── 读取当前执行已有 Observation
  └── 生成本次消费者的不可变投影
```

禁止：

- 用 Memory 恢复 RUNNING Action；
- 把 StateStore 中的所有内容当长期 Memory；
- 让模型输出覆盖 InvocationContext 身份或权限；
- 把 ContextSnapshot 当作可变会话数据库。

### 7.1 缺失依赖的降级矩阵

| 运行条件 | FAST | PLAN | EXPLORE |
|---|---|---|---|
| 无 ModelProvider | explicit target / RuleRouter 可用 | 仅 StaticPlanner / prebuilt Plan 可用 | MODE_NOT_AVAILABLE |
| 无 MemoryProvider | 可用，MemorySlice 为空 | 可用，MemorySlice 为空 | 仅 profile 明确 `memory_required=false` 时可用 |
| Memory read 暂时失败 | 按 Policy 决定 empty-slice 降级或失败 | 同左 | memory_required=true 时失败，否则带 issue 继续 |
| Memory write 失败 | 已完成业务结果不回滚，返回 memory issue | 同左 | 同左 |

一期真实试用环境必须配置 SQLite MemoryProvider；上表只定义可预测的精简装配和故障行为，不把
Memory 变成所有既有 FAST / PLAN 请求的强依赖。

## 8. 实施步骤

### Step F1 — Routing correctness

- deterministic-first RoutingPipeline；
- 模型只返回未知字段；
- Router / Planner 共用 strict structured generation adapter；
- `HYBRID` 和未配置 `EXPLORE` 继续 fail-closed。

**实施状态：已完成（2026-09-01）。** 当前 `RoutingPipeline` 只把类型化
`HARNESS.ROUTE.NO_MATCH` 解释为模型 fallback 条件，其他静态错误原样传播。Pipeline 构造时
拒绝带内部 fallback 的确定性 Router，避免双重或隐藏降级。LLMRouter route-v2
不再让模型返回完整 `RouteDecision`：AUTO 模糊时使用 route-intent Draft；FAST 已知时使用仅含
Capability 的 Draft；显式 target、固定 PLAN 与单一 PLAN Policy 由 Harness 直接物化。模型
Prompt 不包含 requested/effective mode，输出 Schema 不包含 `source`、`route_type`、Planner、
Provider、Plugin 或 metadata。LLMRouter 与 LLMPlanner 均通过同一
`StructuredGenerationAdapter` 进入 REQUIRED structured generation；`EXPLORE` / `HYBRID`
可用性边界未改变。

### Step F2 — Context Contracts 与 Pipeline

- 新增 context contracts、source、assembler、policy、projector；
- 先接入 Router 与 Planner；
- 完成稳定 hash、Policy 后 Snapshot、信任优先级、redaction 和 deterministic truncation tests。

**实施状态：已完成（2026-09-01）。** 已新增 Context Contracts 与 `harness-context`；默认
ContextPipeline 收集 Request/Capability Catalog，经固定排序、去重、基础 trust/sensitivity/expiry
guard 和共享 `PolicyEngine.PRE_CONTEXT` 后才物化 Snapshot，再生成 ROUTE/PLAN 最小 Projection。
LLMRouter/LLMPlanner 缺少对应 Projection 时在 ModelGateway 调用前 fail-closed；模型 Prompt
不携带 request/use/snapshot/trace/provider/plugin identity，只有受信 SYSTEM instruction 可进入
system message，用户与数据文本不能自我提升。ROUTE/PLANNER Trace 只记录稳定 hash 与有界数量；
确定性 hash、redaction、Policy-before-Snapshot、consumer view、truncation 和全仓回归已通过。

### Step F3 — Memory Foundation

- 新增 memory contracts、SPI、Gateway；
- 实现 InMemory / SQLite；
- 接入 ContextAssembler；
- 完成隔离、TTL、scope-checked get/delete、幂等 create、来源和 write-policy tests。

**实施状态：已完成（2026-09-01）。** 已新增 Memory Contracts、Provider SPI、MemoryGateway、
MemoryPolicy、Request evidence resolver，以及 InMemory / SQLite Provider。Gateway 只从可信
InvocationContext 派生 tenant/subject，模型 Draft 不能携带 identity、namespace、sensitivity、
retention 或持久化 ID；Proposal canonical hash、evidence、Secret、TTL 和硬字节上限均在 Provider
outbound 前验证。get/delete 先加载记录再进行 scope/namespace 与 PRE_MEMORY Policy 检查；search
由 Gateway 对 Provider 结果重新过滤、稳定排序、按 query.limit/128 KiB 裁剪并标记 truncated。
`MemoryContextSource` 经 Gateway 获取 MemorySlice，只生成 DATA tier ContextItem，Router/Planner
仍只消费 Projection。默认装配不配置 MemoryProvider，FAST/PLAN 保持可用；SQLite 跨实例持久化、
隔离、删除、TTL、幂等/冲突、Policy、大小边界和跨请求 Context 命中均已有测试。

### Step F4a — Minimal Explore Contracts

- 只实现本说明书 6.1–6.4 的最小集合；
- 不从旧高阶草案复制 Patch、Approval、Async 或 lease 字段；
- 完成 node kind 互斥、profile snapshot、completion eligibility、wire round-trip 与非法字段拒绝测试。

**实施状态：已完成（2026-09-01）。** 已新增 `CapabilityCompletionMode` 兼容字段、Minimal
Explore Contracts、`PlanNodeKind.EXPLORATION` / `ExplorationNodeSpec` 与
`PlanExecutionState.explorations`。`harness-agentic` 已实现可信 ProfileSnapshot 物化、canonical
profile/scope/action/result hash、repeated-action fingerprint、standalone wrapper 结构工厂和
checkpoint integrity validator。Explore allowlist 只接受 AGENT/TOOL 且
`side_effect ∈ {NONE, READ}`、`egress ∈ {NONE, INTERNAL}`、显式 `completion_mode=SYNC`；旧
Descriptor 默认 UNKNOWN，只从 Explore 排除。Turn Draft 不能携带运行身份、Provider/Plugin、
idempotency 或 Patch 字段；outer/inner profile/status/result/completed_at 不一致统一归类为
`HARNESS.EXPLORATION.CHECKPOINT_CORRUPT`。模型 PlanDraft 不暴露 EXPLORATION kind；默认
PlanValidator 仍以 `PLAN.EXPLORATION_NOT_AVAILABLE` 阻断执行，因此本步没有启用模型循环、Action
outbound、Approval/Async 或 HYBRID。

### Step F4b — Minimal Explore Loop

- 将已验证的 ExplorationPlanFactory 与单 `EXPLORATION` 节点接入 EXPLORE handle；
- ExplorationEngine、ScopedActionExecutor、Observation；
- completed-Observation resume；
- outer/inner atomic terminal 与 unexpected ACCEPTED orphan tests；
- Context / Memory / Action 全链路 Trace。

**实施状态：已完成（2026-09-01）。** `build_harness()` 仅在配置至少一个可信
`ExplorationProfile` 且显式声明 `single_writer_guaranteed=True` 时开放 EXPLORE；否则
`RouteDecisionValidator` 和 `PlanValidator` 继续 fail-closed。RequestCoordinator 通过
`ExplorationPlanFactory -> PlanMaterializer.materialize_harness()` 只创建一份 fresh 单节点 Plan，
不调用 Planner，也不把 Harness-owned wrapper 记作 Planner 输出。

`ExplorationEngine` 已实现 REQUIRED structured turn、每次模型 outbound 前 ContextUse/model-call
checkpoint、有限 repair、基础预算与 repeated-action guard。`ScopedActionExecutor` 在 proposal
checkpoint 前后重验可信 scope、类型、JSON Schema、`NONE/READ + NONE/INTERNAL + SYNC`，所有
outbound 只经 `CapabilityInvoker`。普通 SUCCESS/PARTIAL/FAILED Action 与有界 Observation、
`pending_action_id` 清理在同一 checkpoint 完成；Policy DENY/approval、取消和意外 ACCEPTED 均为
终态，意外 ACCEPTED 保存原结果并标记 `orphaned`，返回
`HARNESS.EXPLORATION.ASYNC_CONTRACT_VIOLATION`，不进入 WAITING/callback/retry。

恢复会先重验 outer/inner、profile/scope/proposal/result hash 和计数，只从 completed Observation
边界继续；`PROPOSED/RUNNING` action 稳定返回 `HARNESS.EXPLORATION.RESUME_UNSAFE`。EXPLORE
Context 已包含 prior Observation，Memory-required profile 在缺少 Memory source 时模型调用为零；
EXPLORATION/ACTION Span 仅记录 ID、hash、计数和状态，不复制 Prompt、输入或输出。HYBRID、
PlanPatch、Approval waiting、Async waiting 和分布式 lease 仍未开放。

### Step F5 — Real-use Gate

- FAST、PLAN、standalone EXPLORE 各有至少一个真实业务调用；涉及模型的路径使用真实 ModelProvider；
- 完成一次跨请求 Memory write → 新请求 read → ContextProjection 命中的真实场景；
- 记录 groundedness、重复动作、memory hit、人工修正和错误分类；
- 根据实际失败归因决定继续优化 Context/Memory，还是提出某个高阶 ADR。

**实施状态：Gate 已就绪，真实调用证据待执行（2026-09-01）。** 已新增
`OpenAIResponsesModelProvider`，通过官方 `openai.AsyncOpenAI.responses.create()` 映射
provider-neutral messages、`store=false`、usage/refusal 与安全错误分类。兼容 Schema 使用 strict
`text.format=json_schema`；含 map-valued `additionalProperties` 的跨 Provider Schema 使用
`json_object`，完整原始 Schema 仍由 ModelGateway 在结果进入调用方前强制校验。API key 只存在于
SDK client 内存和 Authorization header，不进入 Descriptor、Trace、Result 或报告。

真实财经场景由业务插件 `finance.portfolio-risk/v1` 承担：使用 Decimal 对调用方时点持仓计算
净资产、日损益、持仓权重、集中度及日亏损限额，不访问行情网络、不提供投资建议，执行画像为
`NONE + NONE + SYNC`。`financeclaw_real_use.gate` 会在同一次评测中执行 FAST、真实模型 PLAN、
standalone EXPLORE，并先完成 Memory write，再在新 EXPLORE 请求中审计
MemoryRecord → ContextUseRecord 命中及 Action 对风险偏好的实际应用。

版本化报告记录各模式 status、groundedness、模型/Action Span 数、repeated action、memory hit、
human correction 和错误分类，不保存 Prompt 或原始响应。默认 pytest 使用官方 SDK 与
`httpx.MockTransport`，
报告强制标记 `live=false` / `gate_passed=false`；只有显式 `--live`、真实 API key/model 且报告
`gate_passed=true` 的运行才能作为本步骤完成证据。当前环境未配置 `OPENAI_API_KEY`，所以 F5
不标记为完成。

## 9. 一期验收

一期完成必须同时满足：

1. FAST / PLAN 无回归；
2. Router、Planner、Explorer 只消费对应 ContextProjection；
3. Secret、Provider identity、Trace baggage 不进入 Prompt；
4. Memory 具备 tenant/subject/namespace 隔离、TTL 和删除；
5. Memory write 必须经过 proposal、evidence validation 和 Policy；
6. standalone EXPLORE 只创建一份单节点 Plan，不逐轮建 Plan；
7. Explore 每轮最多一个 `NONE/READ + NONE/INTERNAL egress + SYNC` Action，且只经 CapabilityInvoker；
8. 基础次数限制和 repeated-action guard 可验证；
9. 只在 completed Observation 边界安全恢复，其他中间态稳定 fail-closed；
10. `HYBRID`、PlanPatch 和高阶预算没有进入当前运行路径；
11. FAST / PLAN / standalone EXPLORE 均有真实记录，并形成可归因的失败样本；
12. 跨请求 Memory write/read 在真实场景验证，且 scope、evidence、delete、TTL 均可审计；
13. outer node / Exploration terminal 原子一致，unexpected ACCEPTED 能稳定进入 ORPHANED/FAILED。

## 10. 后续能力进入条件

HYBRID、PlanPatch、高阶资源预算、复杂分布式恢复、Replay 或 Workflow 自动化都不能因为旧文档
已有 Contract 就进入开发。每项能力必须单独满足：

1. 引用一期真实运行证据；
2. 说明现有 Context、Memory、FAST、PLAN、standalone EXPLORE 为什么不足；
3. 给出最小增量方案，而不是一次性启用整套高阶架构；
4. 新增 ADR、迁移方案和独立验收 Gate。

---

> **一期原则**：先让 Agent 得到正确上下文、记住可信事实并安全完成一个最小行动闭环，再用真实证据决定是否需要更复杂的编排。
