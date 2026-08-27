# FinanceClaw Stage 3A 实施说明书

> **阶段名称**：Stage 3A — Provider Fabric  
> **文档定位**：第三阶段 3A 实施说明书  
> **前置基线**：Stage 1 Minimal Harness + Stage 2 Reliable Plan Execution Engine  
> **上位设计**：Stage 3 — Adaptive Multi-Provider & Agentic Orchestration  
> **核心目标**：在不破坏 Stage 1 / Stage 2 行为和恢复安全性的前提下，让同一个 Capability 支持多个 Provider，并完成可靠选择、Retry / Fallback、Health、Pinning、Canary、ModelProvider 和可恢复 Provider 状态。

---

## 1. 3A 要解决什么

Stage 2 的执行模型是：

```text
Capability
    ↓
唯一 Provider
    ↓
CapabilityInvoker
```

Stage 3A 要升级成：

```text
Capability
   ├── Provider A
   ├── Provider B
   └── Provider C
          │
          ▼
   Provider Selection
          │
          ▼
   CapabilityInvoker
          │
          ▼
   Retry / Fallback
```

3A 不做 Router、LLM Planner、ReAct、Hybrid。  
这些能力都依赖 Provider Fabric，应该在 3A 稳定后再进入 3B / 3C。

### 3A 完成后的核心能力

- 同一 `capability_id` 可以注册多个 Provider。
- Planner / ExecutionPlan 仍然只依赖 `capability_id`。
- Harness 可以根据 Policy、Health、Tenant、Priority、Pin、Canary 选择 Provider。
- Retry 与 Fallback 有清晰不同的执行语义。
- WRITE 跨 Provider fallback 默认 fail-closed。
- Provider selection 可以进入 checkpoint，进程重启后不会错误切换 WRITE Provider。
- ModelProvider 使用同一 Provider Fabric，但通过独立 `ModelGateway` 调用。

---

## 2. 3A 必须先冻结的 4 个设计决定

这四点建议在编码前冻结，后续实现都围绕它们展开。

### 2.1 Capability 和 Provider 分离

```text
capability_id = data.query/v1

provider_id = finance-query-primary
provider_id = finance-query-backup

plugin_id = finance-query-plugin
```

含义：

- `Capability`：系统“能做什么”。
- `Provider`：这次由“谁来做”。
- `Plugin`：Provider 属于哪个生命周期 / 部署单元。

Planner、PlanValidator、ExecutionPlan 继续只认识 `capability_id`。

### 2.2 CapabilityDescriptor 继续作为能力语义唯一真相

现有：

```python
Capability.descriptor() -> CapabilityDescriptor
```

保持不变。

`side_effect / egress / idempotency` 继续属于 Capability，而不是 Provider。

不能出现：

```text
data.query/v1

Provider A = READ
Provider B = WRITE
```

同一 Capability 的多个 Provider 必须具有兼容的 `CapabilityDescriptor`。

### 2.3 Retry 不切 Provider，Fallback 才切 Provider

```text
Retry:
A → A → A

Fallback:
A → B → C
```

因此 Provider 执行必须形成两层状态：

```text
ProviderAttempt A
    retry 1
    retry 2
    retry 3

ProviderAttempt B
    retry 1
```

不能让 Scheduler 每次 Retry 都重新做 Selection。

### 2.4 Provider selection 必须进入 Plan checkpoint

这是 3A 最重要的可靠性要求。

如果：

```text
WRITE Node
↓
选择 Provider A
↓
A 已收到请求
↓
进程 crash
```

重启后不能重新自由选择 B。

至少要持久化：

```text
selected_provider_id
provider_attempt
retry_attempt
selection_key
equivalence_group
```

Resume 默认继续原 Provider。

只有真正满足 WRITE fallback 安全条件时，才允许从 A 切到 B。

---

# 3. 推荐实施顺序

3A 建议拆成 **8 个连续实施步骤**。

顺序不要打乱，因为后一步都依赖前一步形成的稳定边界。

```text
1. Provider Contracts
        ↓
2. Registry 1:N
        ↓
3. Selection / Health
        ↓
4. CapabilityInvoker 接入 Selection
        ↓
5. Retry / Fallback 重构
        ↓
6. Checkpoint / Resume Provider Safety
        ↓
7. Pinning / Canary / Events
        ↓
8. ModelProvider / ModelGateway
        ↓
9. Stage 1 / 2 / 3A 全量回归
```

下面逐步展开。

---

## Step 1 — Provider Contracts

### 目标

先冻结跨模块协议，不先动 Runtime。

### 新增 Contract

建议在 `harness-contracts` 增加：

```text
ProviderDescriptor
ProviderHealthStatus
ProviderHealthSnapshot

ProviderPin

SelectionContext
SelectionDecision
SelectionRejection

ProviderAttempt
```

### ProviderDescriptor 建议字段

```python
ProviderDescriptor(
    provider_id,
    capability_id,
    plugin_id,
    implementation_version,
    priority,
    tags,
    region,
    tenant_visibility,
    equivalence_group,
    metadata,
)
```

注意：

`ProviderDescriptor` 不重复保存 `side_effect / idempotency / egress`。

这些继续来自 `CapabilityDescriptor.execution_profile`。

### Error Model 同时扩展

现有：

```python
retryable: bool
```

3A 建议增加：

```python
fallbackable: bool = False
```

因为：

```text
retryable
    → 是否再次调用当前 Provider

fallbackable
    → 是否允许切换到其他 Provider
```

### 完成标准

- Contract 可以稳定序列化 / 反序列化。
- Contract immutable 规则与现有 `ContractModel` 一致。
- Provider / Selection 错误码冻结。
- 不修改现有 AgentSPI / ToolSPI。

---

## Step 2 — Registry 从 1:1 升级为 1:N

### 当前问题

当前 Registry 本质上是：

```python
_entries: dict[capability_id, ResolvedCapability]
```

所以同一个 Capability 只能注册一个实现。

### 目标结构

建议升级为：

```text
providers_by_id
    provider_id → ProviderRegistration

providers_by_capability
    capability_id → provider_ids[]

capability_descriptors
    capability_id → canonical CapabilityDescriptor
```

### 推荐 API

```python
register_provider(...)

unregister_provider(provider_id)

get_provider(provider_id)

candidates(capability_id)

list_providers(...)

get_capability_descriptor(capability_id)
```

Registry 只回答：

> 有哪些 Provider？

不回答：

> 这次应该选哪个 Provider？

### CapabilityCatalog 必须保持不变

即使：

```text
data.query/v1
├── provider-a
├── provider-b
└── provider-c
```

`RegistryCapabilityCatalog` 仍然只向 Planner / PlanValidator 暴露：

```text
data.query/v1
```

不能暴露 Provider instance。

### LocalPluginLoader 必须一起修改

目前 Loader unload 逻辑是按 capability 注销。

1:N 后必须改成：

```text
register provider_id
unregister provider_id
```

否则卸载一个 Plugin 可能把另一个 Plugin 对同一 Capability 的 Provider 一起移除。

### 旧插件兼容

不修改现有：

```python
Capability.descriptor()
PluginSPI.capabilities()
PluginManifest.capabilities
```

对于没有显式 provider_id 的旧插件，由 Loader 生成稳定 ID，例如：

```text
{plugin_id}:{capability_id}
```

### 完成标准

必须通过：

```text
同 capability 注册 A/B

A/B provider_id 唯一

CapabilityDescriptor 不一致时拒绝注册

unregister A 不影响 B

Plugin unload 只移除自己的 Provider

Catalog 仍然只返回一个 CapabilityDescriptor
```

---

## Step 3 — Selection 与 Health

### 新增模块

```text
harness-selection
```

第一版不要做复杂算法，只把边界做好。

### Selection Pipeline

统一：

```text
Registry candidates
        ↓
Eligibility
        ↓
Ranking
        ↓
Selection
```

### Eligibility 第一版支持

```text
Capability compatibility
Tenant visibility
Policy constraints
Health
Provider Pin
```

### 第一版 Selector

只实现：

```text
PrioritySelector
```

排序建议：

```text
Health
↓
Priority
↓
provider_id 作为稳定 tie-break
```

后面再加 Canary。

### Health 第一版

支持：

```text
HEALTHY
DEGRADED
UNHEALTHY
UNKNOWN
```

第一版 HealthSource：

```text
StaticHealthSource
TestHealthSource
```

Passive Health 可以在 Step 7 完善。

默认：

```text
HEALTHY   → eligible
DEGRADED  → eligible，但降低排名
UNKNOWN   → eligible
UNHEALTHY → reject
```

### SelectionDecision 至少记录

```text
selected_provider_id
eligible_candidates
rejected_candidates + reason_code
selector
reason_code
selection_key
```

### 完成标准

必须验证：

```text
priority selection
health filtering
tenant filtering
policy filtering
deterministic tie-break
no eligible candidate
```

此时仍然先不要改 Scheduler。

---

## Step 4 — CapabilityInvoker 接入 Selection

### 当前路径

```text
CapabilityInvoker
↓
Registry.resolve(capability)
↓
PRE_EXECUTE
↓
Provider
```

### 修改为

```text
CapabilityInvoker
↓
Registry.candidates(capability)
↓
SelectionContext
↓
ProviderSelector
↓
Selected Provider
↓
PRE_EXECUTE
↓
Provider
```

这里先完成“能选择 Provider”，还不要一次把复杂 fallback 全塞进去。

### Policy

`PRE_EXECUTE` 继续保留。

建议让 `PolicyContext` 能看到：

```text
CapabilityDescriptor
ProviderDescriptor
```

这样 Policy 可以表达：

```text
允许 data.query/v1
但禁止 provider-x
```

### 兼容目标

Stage 1 Direct Invocation：

```python
app.invoke(request)
```

必须保持行为兼容。

Stage 2 Scheduler 继续只调用：

```python
CapabilityInvoker.invoke(...)
```

不得访问 Registry / Selector / Provider。

### 完成标准

```text
Direct Invocation 默认选 priority Provider

Plan Node 通过相同路径执行

Policy 可以针对 Provider 拒绝

Stage 1 existing tests 继续通过

Stage 2 基础执行 tests 继续通过
```

---

## Step 5 — Retry / Fallback 重构

这是 3A 最大的 Runtime 改动。

### 当前问题

Stage 2 Retry 在 Scheduler。

如果每一次 Retry 都重新经过新的 Selection，则可能出现：

```text
retry 1 → A
retry 2 → B
retry 3 → A
```

这会把 Retry 与 Fallback 混为一谈。

### 推荐职责调整

```text
Scheduler
    ↓
CapabilityInvoker
    ↓
ProviderExecutionCoordinator
```

建议在 `harness-runtime` 新增类似：

```text
provider_execution.py
```

负责：

```text
ProviderAttempt
same-provider retry
fallback
deadline
error normalization
```

### Scheduler 继续负责

```text
DAG
Node lifecycle
Node budget
Node failure propagation
Checkpoint
Cancellation
```

Scheduler 给 Invoker 提供：

```text
RetryPolicy
Deadline
IdempotencyKey
Cancellation
Progress callback
```

但不选择 Provider。

### Provider Execution 流程

```text
select A
↓
ProviderAttempt A
    retry 1
    retry 2
    retry 3
↓
fallback allowed?
↓
select next eligible Provider B
↓
ProviderAttempt B
    retry 1
```

### WRITE fallback 规则

只有同时满足：

```text
stable idempotency key

source equivalence_group 非空

target equivalence_group 非空

source group == target group

failure fallbackable

target Provider eligible
```

才允许：

```text
A → B
```

否则：

```text
HARNESS.PROVIDER.FALLBACK_UNSAFE
```

### 完成标准

必须通过：

```text
A transient fail → A retry → success

A retry exhausted → B success

A/B 都失败 → terminal failure

retry 永远保持同一 Provider

READ 可以 fallback

WRITE 非幂等禁止 fallback

WRITE 同 group + stable key 才允许 fallback
```

---

## Step 6 — Checkpoint / Resume Provider Safety

这一步不能省略。

否则 3A 会破坏 Stage 2 最重要的 crash recovery 保证。

### NodeExecutionState 建议扩展

至少：

```text
selected_provider_id

provider_attempt
provider_retry_attempt

provider_selection_key

provider_equivalence_group
```

可以再增加：

```text
provider_history
```

但不是第一版必须。

### Checkpoint 时机

至少在：

```text
Provider 已选择
Provider 真正调用前
Retry 前
Fallback 切换前
Provider attempt 完成后
```

形成稳定状态。

### Resume 规则

#### NONE / READ

```text
之前选 A
↓
优先 replay A
↓
A 再失败
↓
可以受控 fallback B
```

#### WRITE 非幂等

保持 Stage 2：

```text
HARNESS.PLAN.RESUME_UNSAFE
```

#### WRITE 幂等

默认：

```text
replay 原 Provider A
```

只有满足 equivalence rule 才允许之后 fallback B。

### 关键原则

禁止：

```text
RUNNING Node
↓
process restart
↓
重新自由做 Priority / Canary Selection
```

因为这可能改变已经处于副作用不确定状态的 Provider。

### 完成标准

必须新增 restart tests：

```text
READ selected A → crash → resume A

WRITE non-idempotent → crash → fail closed

WRITE idempotent → crash → replay original A

WRITE same equivalence group → A replay/fail → controlled B fallback

WRITE different group → B calls == 0
```

---

## Step 7 — Pinning、Canary、Passive Health、Trace / Events

在核心执行安全完成之后，再加流量治理功能。

### Provider Pin

新增正式：

```text
ProviderPin(provider_id)
```

`RequestTarget.plugin` 暂时保留，只作为：

```text
candidate constraint
```

不再作为精确 Provider identity。

Pin 适用于：

```text
debug
test
admin
replay
```

Pin 不能绕过：

```text
Policy
Tenant visibility
Capability compatibility
Health hard rejection
```

### Stable Canary

新增：

```text
WeightedCanarySelector
```

不要使用随机数。

推荐：

```text
hash(tenant_id + stable_subject + capability_id)
```

得到稳定 bucket。

目标：

```text
同一主体稳定
进程重启稳定
可 replay
```

### Passive Health

Provider infrastructure failure 才影响 Health。

例如：

```text
timeout
connection failure
provider unavailable
invalid protocol result
```

业务结果：

```text
account not found
rule rejected
empty result
```

不能直接标记 Provider unhealthy。

### Trace

新增 Provider selection 相关 Span / Event。

建议新增：

```text
SpanType.PROVIDER_SELECT
```

并记录：

```text
provider_id
capability_id
selection_key
provider_attempt
retry_attempt
```

### Events

至少：

```text
provider.candidates
provider.selected
provider.retrying
provider.fallback
provider.failed
provider.health_changed
```

### 完成标准

```text
stable canary

pin exact Provider

pin cannot bypass Policy

unhealthy Provider rejected

degraded Provider lower priority

fallback trace 可解释

selection result 可 replay
```

---

## Step 8 — ModelProvider / ModelGateway

ModelProvider 放在 Provider Fabric 最后实现。

不要在 Registry / Selection / Fallback 尚未稳定时提前做。

### 新增模块

```text
harness-model
```

### 核心接口

```python
class ModelProvider(ABC):

    async def generate(
        self,
        request: GenerateRequest,
        context: InvocationContext,
    ) -> GenerateResult:
        ...
```

### ModelGateway

```text
LLMRouter / LLMPlanner / ExplorationEngine
                │
                ▼
           ModelGateway
                │
                ▼
        Shared Provider Fabric
                │
                ▼
          ModelProvider
```

### 关键决定

ModelGateway：

```text
共享：
Registry
Selection
Health
Fallback
Trace conventions
```

但：

```text
不强制经过 CapabilityInvoker
```

原因是模型调用天然有：

```text
GenerateRequest
Structured Output
Usage
Token Count
Finish Reason
Model Parameters
```

不应强制伪装成 AgentRequest / ToolRequest。

### 第一版实现

```text
MockFastModel
MockQualityModel
MockBackupModel
```

第一版支持：

```text
generate
structured output
usage metadata
timeout
fallback
provider identity
```

暂不支持：

```text
streaming
vision
embedding
rerank
```

### 完成标准

```text
quality model selected

quality model timeout

same-provider retry

fallback backup model

structured output

usage metadata

provider trace
```

完成后 Stage 3B 可以直接实现：

```text
LLMRouter
LLMPlanner
```

而不用再次重构模型 Provider 基础设施。

---

# 4. 推荐模块修改顺序

按代码模块看，建议以下顺序推进：

```text
1. harness-contracts
        ↓
2. harness-registry
        ↓
3. harness-plugin-local
        ↓
4. harness-selection   [NEW]
        ↓
5. harness-runtime
        ↓
6. harness-execution
        ↓
7. harness-policy
        ↓
8. harness-trace / harness-events
        ↓
9. harness-model       [NEW]
        ↓
10. harness-bootstrap
```

其中最危险的改动集中在：

```text
harness-runtime/invoker.py

harness-execution/scheduler.py

harness-execution/recovery.py
```

这三个模块必须作为一组设计，不能各自独立修改。

---

# 5. 推荐提交拆分

建议按以下 commit / PR slice 实施。

## Commit 1 — Provider Contracts

```text
ProviderDescriptor
SelectionContext / SelectionDecision
ProviderHealthSnapshot
ProviderAttempt
Provider error codes
```

不改 Runtime。

---

## Commit 2 — Registry 1:N

```text
ProviderRegistration
register_provider
unregister_provider
candidates
CapabilityCatalog adaptation
LocalPluginLoader adaptation
```

保证旧插件继续工作。

---

## Commit 3 — Selection Foundation

```text
harness-selection
Eligibility
PrioritySelector
StaticHealthSource
```

先只写 deterministic selection。

---

## Commit 4 — Invoker Selection Integration

```text
CapabilityInvoker:
resolve → candidates → selector → invoke
```

完成 Stage 1 / Stage 2 基础回归。

---

## Commit 5 — Provider Retry / Fallback

```text
ProviderExecutionCoordinator
ProviderAttempt
fallbackable
WRITE fallback guard
```

把 Retry 和 Fallback 真正分开。

---

## Commit 6 — Provider Checkpoint / Resume

```text
NodeExecutionState provider fields
checkpoint callbacks
Resume original Provider
WRITE restart safety
```

这是 3A 可靠性 Gate。

---

## Commit 7 — Pin / Canary / Passive Health / Observability

```text
ProviderPin
WeightedCanarySelector
PassiveInvocationHealth
Provider trace
Provider events
```

完成流量治理。

---

## Commit 8 — Model Fabric

```text
harness-model
ModelProvider
ModelGateway
Mock models
model fallback
```

为 3B 提供模型基础设施。

---

## Commit 9 — 3A Acceptance

补齐：

```text
multi-provider E2E
fallback fault injection
restart tests
WRITE safety tests
model tests
Stage 1 regression
Stage 2 regression
```

全部通过后结束 3A。

---

# 6. 最终验收场景

3A 最终建议用三条主链路验收。

## 场景 A：READ Provider Fallback

```text
finance.query/v1

primary(priority=100)
backup(priority=50)
```

执行：

```text
select primary
↓
primary transient failure
↓
retry primary
↓
primary transient failure
↓
fallback backup
↓
SUCCESS
```

验证：

```text
Retry 没有切 Provider
Fallback 明确切 Provider
Trace 完整
Plan 结果正确
```

---

## 场景 B：WRITE Safe / Unsafe Fallback

安全：

```text
A equivalence_group=payment-prod
B equivalence_group=payment-prod

stable idempotency_key
```

允许：

```text
A → B
```

不安全：

```text
A group=payment-prod
B group=payment-backup
```

必须：

```text
HARNESS.PROVIDER.FALLBACK_UNSAFE
B.calls == 0
```

---

## 场景 C：Crash / Resume

```text
WRITE Node
↓
select A
↓
checkpoint provider=A
↓
调用期间 process crash
```

重启：

```text
load checkpoint
↓
resume original A
```

禁止：

```text
重新做 Priority / Canary
↓
意外选择 B
```

这个测试通过，才说明 3A 没有破坏 Stage 2 的可靠性基线。

---

# 7. 3A 完成定义

以下条件全部满足后进入 Stage 3B：

- Registry 1:N 稳定。
- CapabilityCatalog 仍然是 capability-only。
- Stage 1 插件无需破坏性修改。
- ProviderSelector 已独立。
- Retry 只发生在当前 Provider。
- Fallback 是明确的 Provider 切换。
- WRITE fallback 已 fail-closed。
- Provider selection 已进入 checkpoint。
- Resume 默认保持原 Provider。
- Pin / Canary / Health 已可用。
- Provider Trace / Events 可解释。
- ModelProvider / ModelGateway 已稳定。
- Stage 1 全量回归通过。
- Stage 2 全量回归通过。
- 3A fault injection / restart tests 通过。

---

# 8. 一句话原则

> **Stage 3A 的重点不是“支持多个 Provider”，而是在 Provider 可替换之后，仍然保持 FinanceClaw 的执行安全、恢复安全和可观测性。**

实现时优先级始终是：

```text
Correctness
    ↓
Recovery Safety
    ↓
Selection
    ↓
Fallback
    ↓
Traffic Governance
    ↓
ModelProvider
```

不要为了 Canary、Health 或 ModelProvider 提前牺牲 Stage 2 已建立的可靠执行边界。
