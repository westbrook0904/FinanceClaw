# FinanceClaw Stage 3B 实施说明书

> **阶段名称**：Stage 3B — Routing & Planning
> **文档定位**：第三阶段 3B 已实施编码基线
> **版本**：V1.0（实现完成）
> **日期**：2026-08-28
> **前置基线**：Stage 1 Minimal Harness + Stage 2 Reliable Plan Execution Engine + Stage 3A Provider Fabric
> **上位设计**：`.design/FinanceClaw-第三阶段说明书.md`
> **核心目标**：在不把执行权交给 Router 或模型的前提下，为 FinanceClaw 增加统一 `handle()` 入口、请求级 ExecutionMode、确定性与模型 Router、结构化 LLM Planner、有限 Plan Repair，并将合法计划继续交给 Stage 2 ExecutionEngine 执行。
> **范围结论**：Stage 3B 实际开放 `AUTO / FAST / PLAN`；`EXPLORE / HYBRID` 只冻结协议并 fail-closed，执行能力留给 Stage 3C。Route / Plan Replay Eval 留给 Stage 3D。
> **实施状态**：Step 1—11 已完成；Stage 1 / 2 / 3A / 3B 统一回归 Gate 已建立并通过。

---

## 1. 3B 要解决什么

Stage 3A 完成后，FinanceClaw 已经具备：

```text
Request with explicit target
        ↓
CapabilityInvoker
        ↓
Provider Selection
        ↓
Retry / Fallback
        ↓
Provider
```

以及：

```text
Caller-provided ExecutionPlan
        ↓
PlanValidator
        ↓
ExecutionEngine
        ↓
Checkpoint / Resume
```

当前仍由调用方决定：

- 是直接调用，还是执行计划；
- 直接调用哪个 Capability；
- 复杂目标应生成什么 ExecutionPlan；
- 模型输出不合法时如何安全修复；
- 路由与规划决策如何进入统一 Trace / Events。

Stage 3B 要补齐：

```text
Request
  ↓
HarnessApplication.handle()
  ↓
PRE_ROUTE Policy
  ↓
Router
  ↓
RouteDecision
  ├── FAST → CapabilityInvoker
  └── PLAN → Planner → PlanValidator → ExecutionEngine
```

### 1.1 3B 完成后的核心能力

- `RequestOptions.execution_mode` 成为请求级稳定协议。
- `HarnessApplication.handle()` 成为上层应用推荐入口。
- 明确目标的请求可以确定性进入 FAST，不需要模型。
- 模糊请求可以由 LLMRouter 生成结构化 RouteDecision。
- LLMRouter 只能选择执行路径和 Capability，不接触 Provider instance。
- LLMPlanner 只能生成结构化 Plan，不执行任何业务 Capability。
- 非法 Plan 可以根据结构化 Validator issues 进行有限修复。
- 所有 Plan 在进入 ExecutionEngine 前都必须通过 PlanValidator。
- Route、Planner、Model、Plan、Provider Trace 可以形成一条父子链。
- Stage 1 `invoke()`、Stage 2 `execute_plan()` 与 Stage 3A Provider 行为保持兼容。

---

## 2. 3B 范围与非目标

### 2.1 本阶段必须实现

```text
ExecutionMode contract
RouteType / RouteDecision contract
RequestOptions.execution_mode
Router SPI
RuleRouter
LLMRouter
RouteDecision validation
PRE_ROUTE Policy
HarnessApplication.handle()
Planner SPI / local PlannerRegistry
StaticPlanner foundation
LLMPlanner
HybridPlanner
Structured PlanDraft generation
Bounded Plan Repair
Route / Planner Trace and Events
Stage 3B Acceptance
```

### 2.2 本阶段明确不实现

```text
× ExplorationEngine
× Bounded ReAct
× ScopedActionExecutor
× Explore checkpoint / resume
× PlanPatchProposal / Plan revision
× EXPLORE 实际执行
× HYBRID 实际执行
× WorkflowSPI / Workflow Catalog
× Router 或 Planner 直接执行 Capability
× Router 或 Planner 访问 Provider instance
× Router / Plan Replay Eval 框架
× EmbeddingRouter / EnsembleRouter
× Prompt 管理平台
× 厂商模型 SDK 进入 harness-routing / harness-planning
× Provider Pin 外部入口、Weighted Canary、Passive Health
```

`HybridPlanner` 和 `ExecutionMode.HYBRID` 是两个不同概念：

```text
HybridPlanner
    = 确定性 Planner 与 LLMPlanner 的安全组合策略

ExecutionMode.HYBRID
    = Plan 中包含受限 Explore 节点的执行模式
```

3B 实现前者，不实现后者。

### 2.3 Route / Plan Eval 的阶段归属

第三阶段上位说明在 3B 清单中提到 `Route / Plan Eval`，但完整 Replay Eval 已归入 3D。

3B 只负责提供未来 Eval 所需的稳定事实：

```text
request summary hash
catalog snapshot hash
router_id / planner_id
route decision
reason_code
prompt_version
planning attempt
validation issue codes
model provider_id / usage
```

3B 不实现离线 Replay、准确率统计和新旧策略对比执行器。

---

## 3. 编码前建议冻结的 9 个决定

本文后续方案按以下决定设计。讨论完成后，应把它们同步回 Stage 3 ADR 摘要。

### 3.1 ExecutionMode 的唯一持久化位置

采用：

```python
RequestOptions(
    execution_mode=ExecutionMode.AUTO,
)
```

`handle(request, mode=...)` 只是本地 API sugar：

- `mode is None`：使用 `request.options.execution_mode`；
- `mode` 非空且 Request 中是 `AUTO`：复制 Request，并把 mode 归一化进 RequestOptions；
- 两处都指定非 AUTO 且值不同：返回 `HARNESS.REQUEST.MODE_CONFLICT`；
- 原 Request 不做原地修改。

低层 API 的语义保持不变：

```text
app.invoke(request)
    → 始终是 Direct Invocation

app.execute_plan(request, plan)
    → 始终执行调用方提供的 Plan
```

低层 API 不根据 `execution_mode` 改变行为，避免破坏 Stage 1 / 2。

### 3.2 3B 只执行 AUTO / FAST / PLAN

公共枚举一次性冻结为：

```text
AUTO
FAST
PLAN
EXPLORE
HYBRID
```

但 3B 的执行矩阵是：

| 请求模式 | 3B 行为 |
|---|---|
| `AUTO` | Router 选择 FAST 或 PLAN |
| `FAST` | 只允许 Direct Capability 路径 |
| `PLAN` | 只允许 Planner 路径 |
| `EXPLORE` | `HARNESS.ROUTE.MODE_NOT_AVAILABLE` |
| `HYBRID` | `HARNESS.ROUTE.MODE_NOT_AVAILABLE` |

禁止静默降级：

```text
EXPLORE → PLAN
HYBRID  → PLAN
```

否则 3C 上线后，同一个请求会出现不可预测的语义变化。

### 3.3 Router 只产生 RouteDecision

Router 可以：

```text
读取受限 RequestSummary
读取 capability-only Catalog snapshot
读取 PRE_ROUTE constraints
调用 ModelGateway（LLMRouter）
返回 RouteDecision
```

Router 不可以：

```text
调用 CapabilityInvoker
调用 ExecutionEngine
访问 ProviderRegistry 的 Provider instance
选择 provider_id / plugin_id
生成或修改 ExecutionPlan
写 StateStore
```

### 3.4 `handle()` 的调度权属于 Harness

RouteDecision 只是提议，最终由 Harness 校验并分派：

```text
Router
  ↓ proposal
RouteDecisionValidator
  ↓ validated
RequestCoordinator
  ├── FAST → CapabilityInvoker
  └── PLAN → Planner → ExecutionEngine
```

`harness-routing` 不引用 Runtime / ExecutionEngine。

### 3.5 一次 handle 只创建一个请求生命周期

不能简单实现为：

```python
await app.invoke(request)
await app.execute_plan(request, plan)
```

因为现有两个 API 都会各自创建 InvocationContext 和 Request Span。

3B 必须保证：

```text
one normalized Request
one InvocationContext
one effective deadline
one REQUEST span
one RUNTIME(handle) span
```

FAST 与 PLAN 的内部执行入口复用这个 Context 和父 Span。

### 3.6 模型生成 PlanDraft，Harness 生成 ExecutionPlan identity

模型不应控制：

```text
plan_id
revision
provider_id
selection state
approval grant
checkpoint state
```

LLMPlanner 的模型输出使用受限 `PlanDraft` schema。通过解析与验证后，由 Harness 分配：

```text
plan_id = plan_id_factory()
revision = 1
```

最终 Planner API 仍只返回标准 `ExecutionPlan`。

### 3.7 Plan Repair 是有限的生成重试，不是执行重试

默认：

```text
max_plan_attempts = 3
```

它表示：

```text
initial generation + repair generation(s)
```

不等于 ModelGateway 内部的 Provider retry / fallback，也不等于 PlanNode retry。

只有以下输入可以进入 repair：

```text
上一轮结构化输出
parse error summary
PlanValidator issue codes
Policy-visible planning constraints
同一份 CapabilityCatalog snapshot
```

任何 repair 期间都不能执行 Capability。

### 3.8 PRE_ROUTE 第一版不创建审批等待态

PRE_ROUTE 第一版支持：

```text
ALLOW
DENY
force mode constraint
allowed modes constraint
allowed capability IDs constraint
allowed planner IDs constraint
planning limits
```

如果 Policy 在 PRE_ROUTE 返回 `REQUIRE_APPROVAL`，3B 采用 fail-closed：

```text
HARNESS.ROUTE.APPROVAL_NOT_SUPPORTED
```

原因是路由发生在 Plan 创建前，当前 StateStore 没有独立 Route Waiting 状态。不能伪造一个无法安全 resume 的审批 continuation。

需要路由前审批时，应在 3C 或后续阶段单独设计 Request-level checkpoint；Plan 内审批仍继续由 Stage 2 Approval Node 处理。

### 3.9 HybridPlanner 只在 NOT_APPLICABLE 时切换 Planner

推荐语义：

```text
Static / Rule Planner
    ├── applicable → validated ExecutionPlan
    └── NOT_APPLICABLE → LLMPlanner
```

禁止：

```text
primary Planner 产生非法 / 不安全 Plan
        ↓
静默 fallback 到另一个 Planner
```

只有明确的 `HARNESS.PLANNER.NOT_APPLICABLE` 可以触发 Planner 切换。验证失败、Policy 拒绝、超时和模型失败必须保留原错误语义。

---

## 4. 目标架构

```text
                         HarnessApplication.handle()
                                      │
                                      ▼
                          normalize execution_mode
                                      │
                                      ▼
                         create InvocationContext once
                                      │
                                      ▼
                              PRE_ROUTE Policy
                                      │
                                      ▼
                               RequestCoordinator
                                      │
                                      ▼
                                   Router
                    ┌─────────────────┴─────────────────┐
                    │                                   │
               RuleRouter                          LLMRouter
                    │                                   │
                    │                              ModelGateway
                    └─────────────────┬─────────────────┘
                                      ▼
                              RouteDecisionValidator
                                      │
                    ┌─────────────────┴─────────────────┐
                    ▼                                   ▼
              FAST / DIRECT                       PLAN / PLANNER
                    │                                   │
                    ▼                                   ▼
           CapabilityInvoker                    PlannerRegistry
                    │                                   │
                    ▼                         ┌─────────┴─────────┐
           Provider Fabric                    ▼                   ▼
                                       StaticPlanner        LLMPlanner
                                                                │
                                                          ModelGateway
                                                                │
                                                                ▼
                                                            PlanDraft
                                                                │
                                                      parse / validate / repair
                                                                │
                                                                ▼
                                                          ExecutionPlan
                                                                │
                                                                ▼
                                                          PlanValidator
                                                                │
                                                                ▼
                                                         ExecutionEngine
```

父子 Trace 建议：

```text
REQUEST
└── RUNTIME(handle)
    ├── POLICY(pre_route)
    └── ROUTE
        └── MODEL                         # 仅 LLMRouter
    ├── CAPABILITY / PROVIDER_SELECT      # FAST
    └── PLANNER                           # PLAN
        ├── MODEL                         # 每次 generation / repair
        └── PLAN
            └── SCHEDULER / PLAN_NODE ...
```

---

## 5. Contracts

### 5.1 ExecutionMode

建议新增：

```python
class ExecutionMode(StrEnum):
    AUTO = "auto"
    FAST = "fast"
    PLAN = "plan"
    EXPLORE = "explore"
    HYBRID = "hybrid"
```

位置：

```text
harness-contracts/src/harness_contracts/routing.py
```

并扩展：

```python
class RequestOptions(ContractModel):
    timeout_ms: int | None = Field(default=None, gt=0)
    trace: bool = True
    execution_mode: ExecutionMode = ExecutionMode.AUTO
```

旧 Request JSON 没有该字段时自动得到 `AUTO`，因此向后兼容。

### 5.2 RouteType

```python
class RouteType(StrEnum):
    DIRECT_CAPABILITY = "direct_capability"
    GENERATED_PLAN = "generated_plan"
    EXPLORATION = "exploration"
    HYBRID = "hybrid"
```

`ExecutionMode` 表示调用方要求 / 最终选择的模式；`RouteType` 表示 Harness 要分派的执行路径。两者不是自由组合。

### 5.3 RouteSource

```python
class RouteSource(StrEnum):
    REQUEST = "request"
    POLICY = "policy"
    RULE = "rule"
    MODEL = "model"
```

它用于解释决策来源，不授予任何执行权限。

### 5.4 RouteDecision

建议字段：

```python
class RouteDecision(ContractModel):
    mode: ExecutionMode
    route_type: RouteType
    source: RouteSource
    capability_id: NonEmptyString | None = None
    explorer_id: NonEmptyString | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reason_code: NonEmptyString
    metadata: FrozenJsonMapping = Field(default_factory=dict)
```

必须满足：

| mode | route_type | 必填 | 禁止 |
|---|---|---|---|
| FAST | DIRECT_CAPABILITY | capability_id | explorer_id |
| PLAN | GENERATED_PLAN | — | capability_id、explorer_id |
| EXPLORE | EXPLORATION | explorer_id | capability_id |
| HYBRID | HYBRID | explorer_id | capability_id |
| AUTO | 不允许出现在最终 Decision | — | — |

3B 的 Validator 在结构正确后还要检查：

- FAST capability 必须存在于本次 CapabilityCatalog snapshot；
- Router 只能返回 capability_id，不能返回 provider_id / plugin_id；
- PLAN 只表达“需要规划”，不得携带 planner_id；
- Planner 由 Composition Root / RequestCoordinator 根据服务端默认配置和 Policy 约束选择；
- Decision 必须满足 PRE_ROUTE allowed modes / capabilities / planners；
- 3B 收到 EXPLORE / HYBRID Decision 时返回 MODE_NOT_AVAILABLE；
- 固定请求模式不能被 Router 改成其他模式；
- Request 已指定 target 时，模型不能改写成另一个 capability；
- Request target 中既有 plugin 限定只能由调用方保留，模型不能创建新的 plugin pin。

### 5.5 RequestSummary

Router / Planner 不直接序列化完整 InvocationContext。

建议内部协议：

```python
class RequestSummary(ContractModel):
    request_id: NonEmptyString
    input_type: NonEmptyString
    input_content: FrozenJsonValue
    target_capability: NonEmptyString | None = None
    metadata: FrozenJsonMapping = Field(default_factory=dict)
```

生成规则：

- 不包含 IdentityContext、Tenant attributes、Trace baggage；
- metadata 只包含 Composition Root 配置的 allowlist；
- input content 做 JSON 深度、集合大小和字符串长度限制；
- 可注入 `RequestProjector` 做业务脱敏；
- 超过限制返回明确错误，不把任意大对象发送给模型；
- Trace / Event 只记录 summary hash，不记录原始内容。

“safe summary”表示边界明确、大小受限、字段 allowlist，并不自动保证业务内容不含秘密。真实接入外部模型前仍必须由应用配置脱敏与 egress Policy。

### 5.6 RoutingContext

建议放在 `harness-routing`：

```python
class RoutingContext(ContractModel):
    invocation: InvocationContext
    request_summary: RequestSummary
    requested_mode: ExecutionMode
    catalog_snapshot: tuple[CapabilityDescriptor, ...]
    constraints: RoutePolicyConstraints
```

Router 看到 Descriptor，不看到 ProviderRegistration。

### 5.7 RoutePolicyConstraints

第一版使用类型化约束，避免 Router 自行解释任意 dict：

```python
class RoutePolicyConstraints(ContractModel):
    forced_mode: ExecutionMode | None = None
    allowed_modes: frozenset[ExecutionMode] | None = None
    allowed_capability_ids: frozenset[str] | None = None
    allowed_planner_ids: frozenset[str] | None = None
    max_plan_attempts: int | None = Field(default=None, ge=1)
    max_plan_nodes: int | None = Field(default=None, ge=1)
```

多条 Policy 的合并规则：

```text
allowed set  → 交集
numeric max  → 取更小值
forced value → 必须一致，否则 fail-closed
```

不能使用“后一个 Policy 覆盖前一个 Policy”处理安全约束。

### 5.8 Planner SPI

建议：

```python
class Planner(ABC):
    @property
    def planner_id(self) -> str: ...

    async def plan(self, context: PlanningContext) -> ExecutionPlan: ...
```

Planner 的输出边界是已解析、已验证的标准 `ExecutionPlan`。

### 5.9 PlanningContext

```python
class PlanningContext(ContractModel):
    invocation: InvocationContext
    goal: RequestSummary
    catalog_snapshot: tuple[CapabilityDescriptor, ...]
    constraints: PlanningConstraints
```

```python
class PlanningConstraints(ContractModel):
    max_plan_attempts: int = Field(default=3, ge=1)
    max_plan_nodes: int = Field(default=32, ge=1)
    allowed_capability_ids: frozenset[str] | None = None
    deadline_at: datetime | None = None
```

最终 allowed capability 是：

```text
Catalog capability IDs
∩ PRE_ROUTE allowed capability IDs
∩ Planner configuration scope
```

### 5.10 PlanDraft

`PlanDraft` 是模型结构化输出协议，不进入 Runtime / StateStore：

```python
class PlanDraft(ContractModel):
    budget: PlanBudget = Field(default_factory=PlanBudget)
    failure_policy: FailurePolicy = FailurePolicy.FAIL_FAST
    nodes: tuple[PlanNode, ...]
    edges: tuple[PlanEdge, ...] = ()
    outputs: dict[str, OutputBinding] = Field(default_factory=dict)
```

实现时应复用 `ExecutionPlan.outputs` 的冻结与序列化规则；如果需要跨模块类型标注，先把对应 mapping alias 提升为 `harness-contracts` 的公共导出，不从 `plan.py` 私有布局导入。

刻意不包含：

```text
plan_id
revision
metadata
provider identity
execution state
```

Harness 转换为：

```python
ExecutionPlan(
    plan_id=plan_id_factory(),
    revision=1,
    budget=draft.budget,
    failure_policy=draft.failure_policy,
    nodes=draft.nodes,
    edges=draft.edges,
    outputs=draft.outputs,
    metadata={
        "planner_id": planner_id,
        "prompt_version": prompt_version,
        "request_id": request_id,
    },
)
```

### 5.11 PlanningAttempt

用于 Event / Trace / 测试，不持久化隐藏思维过程：

```python
class PlanningAttempt(ContractModel):
    attempt: int = Field(ge=1)
    kind: Literal["initial", "repair"]
    provider_id: NonEmptyString | None = None
    prompt_version: NonEmptyString
    output_hash: NonEmptyString | None = None
    validation_codes: tuple[NonEmptyString, ...] = ()
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
```

不记录：

```text
hidden chain-of-thought
原始完整 prompt
原始完整用户输入
secret / credential
```

---

## 6. Error Model

建议扩展 ErrorCategory：

```text
ROUTE
PLANNER
```

新增错误码：

```text
HARNESS.REQUEST.MODE_CONFLICT

HARNESS.ROUTE.NO_MATCH
HARNESS.ROUTE.INVALID_DECISION
HARNESS.ROUTE.MODE_NOT_ALLOWED
HARNESS.ROUTE.MODE_NOT_AVAILABLE
HARNESS.ROUTE.CAPABILITY_NOT_ALLOWED
HARNESS.ROUTE.PLANNER_NOT_ALLOWED
HARNESS.ROUTE.MODEL_FAILED
HARNESS.ROUTE.APPROVAL_NOT_SUPPORTED

HARNESS.PLANNER.NOT_CONFIGURED
HARNESS.PLANNER.NOT_APPLICABLE
HARNESS.PLANNER.INVALID_OUTPUT
HARNESS.PLANNER.PLAN_TOO_LARGE
HARNESS.PLANNER.REPAIR_EXHAUSTED
HARNESS.PLANNER.DEADLINE_EXCEEDED
HARNESS.PLANNER.MODEL_FAILED
```

建议异常类型：

```python
class RoutingError(HarnessError): ...


class PlanningError(HarnessError): ...


class PlannerNotApplicableError(PlanningError): ...
```

错误传播原则：

- Router / Planner 不返回半合法对象；
- `handle()` 把 HarnessError 转为 ResultEnvelope；
- ModelGateway 原始错误只以安全 cause code 进入 Route / Planner error details；
- 不把模型原始响应放入对外 ErrorDetail；
- repair exhausted 后不创建 Plan checkpoint，也不执行任何节点。

---

## 7. 模块修改范围

### 7.1 `harness-contracts`

新增：

```text
routing.py
    ExecutionMode
    RouteType
    RouteSource
    RouteDecision

request.py
    RequestOptions.execution_mode

errors.py
    ROUTE / PLANNER categories
    route / planner error codes and exceptions
```

顶层 `__init__.py` 导出稳定协议。

### 7.2 `harness-routing`（新增）

建议结构：

```text
harness-routing/
├── README.md
└── src/harness_routing/
    ├── __init__.py
    ├── router.py
    ├── models.py
    ├── rules.py
    ├── llm.py
    ├── validation.py
    ├── projection.py
    └── py.typed
```

职责：

```text
Router SPI
RoutingContext
RequestProjector
RuleRouter
LLMRouter
RouteDecisionValidator
```

禁止依赖：

```text
harness-runtime
harness-execution
Provider instance
业务插件
厂商 SDK
```

允许依赖：

```text
harness-contracts
harness-registry 的 CapabilityCatalog abstraction
harness-model 的 ModelGateway
```

### 7.3 `harness-planning`

当前只有 PlanValidator。3B 扩展：

```text
planner.py
    Planner SPI

registry.py
    local PlannerRegistry

static.py
    StaticPlanner

llm.py
    LLMPlanner

hybrid.py
    HybridPlanner

draft.py
    PlanDraft

generation.py
    bounded generation / repair coordinator
```

PlannerRegistry 是 Harness 内本地策略注册表，不是 Plugin Registry，也不是 Workflow Catalog。

### 7.4 `harness-policy`

扩展：

```text
PolicyPhase.PRE_ROUTE
PolicyContext.requested_mode
RoutePolicyConstraints
安全约束 reducer
```

现有 PRE_PLAN / PRE_EXECUTE 行为必须不变。

### 7.5 `harness-bootstrap`

扩展 HarnessComponents：

```text
router
planner_registry
request_coordinator
```

扩展 build_harness：

```python
build_harness(
    ...,
    router: Router | None = None,
    planners: Iterable[Planner] = (),
    default_planner_id: str | None = None,
    request_projector: RequestProjector | None = None,
)
```

默认使用不依赖模型的 RuleRouter。

`HarnessApplication.handle()` 只做：

```text
检查 application started
归一化 mode sugar
委托 RequestCoordinator
```

不要把 Router prompt、Plan repair loop 和 dispatch 细节堆入 application.py。

### 7.6 `harness-execution`

为 PLAN 路径抽取 context-aware 内部入口：

```python
ExecutionEngine.execute_with_context(
    request,
    plan,
    context,
    *,
    parent,
)
```

公共：

```python
execute(request, plan)
```

继续创建自己的 Context / Request Span，然后委托同一内部核心。

`handle()` 使用 context-aware 入口，避免重复创建生命周期。

### 7.7 `harness-runtime`

FAST 路径可以由 RequestCoordinator 在已有 Context 下直接调用 CapabilityInvoker。

如果为了复用 Direct Invocation 结果归一化而抽取内部方法，必须保证：

```text
invoke(request) 的公共行为不变
CapabilityInvoker 不获得 Router / Planner 依赖
```

### 7.8 `harness-trace`

新增：

```text
SpanType.ROUTE
```

`SpanType.PLANNER` 已存在，继续复用。

### 7.9 `harness-events`

新增：

```text
route.decided
mode.selected
route.failed

planner.started
planner.repairing
planner.completed
planner.failed
```

### 7.10 根项目配置

`pyproject.toml` 增加：

```text
harness_routing package
harness_routing package-dir
harness_routing py.typed data
```

---

# 8. 推荐实施顺序

3B 建议拆成 11 个连续步骤：

```text
1. ExecutionMode / Route Contracts
        ↓
2. Routing Foundation / RuleRouter
        ↓
3. PRE_ROUTE Policy
        ↓
4. handle() FAST Path
        ↓
5. Planner SPI / Registry / Static / Hybrid
        ↓
6. LLMRouter
        ↓
7. PlanDraft / LLMPlanner
        ↓
8. Bounded Plan Repair
        ↓
9. handle() PLAN Path / Shared Lifecycle
        ↓
10. Route / Planner Observability
        ↓
11. Stage 3B Acceptance
```

下面逐步展开。

---

## Step 1 — ExecutionMode / Route Contracts

### 目标

先冻结所有跨模块稳定协议，不接 Model，不改执行路径。

### 实施内容

```text
ExecutionMode
RouteType
RouteSource
RouteDecision
RequestOptions.execution_mode
Routing / Planning errors
```

### Contract 校验

必须覆盖：

```text
old Request JSON → execution_mode=AUTO
AUTO 不能成为最终 RouteDecision
FAST 只能配 DIRECT_CAPABILITY
PLAN 只能配 GENERATED_PLAN
confidence ∈ [0, 1]
不同 route_type 的字段互斥
unknown field rejected
serialization round trip
```

### 完成标准

- `harness-contracts` 顶层可导入新增协议。
- Stage 1 / 2 / 3A Request 构造无需修改。
- 旧序列化数据可加载。
- 还没有任何 Router 执行业务能力。

---

## Step 2 — Routing Foundation / RuleRouter

### 目标

先建立完全确定性的 Router SPI、Decision Validator 和 RuleRouter。

### Router SPI

```python
class Router(ABC):
    @property
    def router_id(self) -> str: ...

    async def route(self, context: RoutingContext) -> RouteDecision: ...
```

### RuleRouter 第一版规则

按顺序匹配，第一条命中即结束：

```text
1. requested mode = EXPLORE / HYBRID
   → 产生对应 Decision，随后由 3B availability guard 拒绝

2. Request 有显式 target，requested mode = AUTO / FAST
   → FAST + DIRECT_CAPABILITY(target.capability)

3. requested mode = PLAN
   → PLAN + GENERATED_PLAN（不选择 Planner）

4. requested mode = FAST 且没有 target
   → 查询显式配置的 input-type → capability rule

5. requested mode = AUTO 且命中配置规则
   → 配置的 FAST 或 PLAN Decision

6. 未命中
   → fallback Router；没有 fallback 时 ROUTE.NO_MATCH
```

RuleRouter 可以组合 LLMRouter 作为最后 fallback，但不能在 LLMRouter 失败后反向猜测另一个执行模式。

### RouteDecisionValidator

Validator 独立于 Router 实现，Application 不信任任何 Router 返回值。

校验：

```text
schema invariant
requested mode invariant
catalog capability existence
planner existence
policy constraints
3B mode availability
request target consistency
```

### 完成标准

- 无模型即可完成显式 target 的 AUTO → FAST。
- Rule 顺序稳定且测试可解释。
- 未命中返回明确错误，不猜测 Capability。
- Router 测试证明没有 CapabilityInvoker / Provider 依赖。

---

## Step 3 — PRE_ROUTE Policy

### 目标

让模式和路由范围在模型决策前接受治理。

### PolicyContext 修改

新增 PRE_ROUTE payload 规则：

```text
required:
    invocation
    requested_mode

forbidden:
    plan
    capability
    provider
    approval_grant
```

现有 PRE_PLAN / PRE_EXECUTE validator 分支保持不变。

### Policy 执行顺序

```text
normalize Request
  ↓
create InvocationContext
  ↓
PRE_ROUTE policies
  ↓
reduce typed constraints
  ↓
derive effective requested mode
  ↓
Router
  ↓
validate Decision against constraints
```

### Fail-closed 情况

```text
DENY
REQUIRE_APPROVAL
forced_mode conflict
allowed_modes empty intersection
invalid constraint payload
Router returns forbidden capability / planner
```

### 完成标准

- Policy 可以强制 AUTO 请求进入 FAST 或 PLAN。
- Policy 可以禁止 PLAN。
- Router 在 Policy deny 时零调用。
- ModelGateway 在 Policy deny 时零调用。
- PRE_PLAN / PRE_EXECUTE 全量回归通过。

---

## Step 4 — `handle()` FAST Path

### 目标

先让统一入口可靠地完成确定性 Direct Invocation。

### 推荐新增组件

```text
harness-bootstrap RequestCoordinator
```

它负责 orchestration，不属于 Router。

### 流程

```text
handle(request, mode?)
  ↓
normalize mode into copied Request
  ↓
create InvocationContext once
  ↓
REQUEST → RUNTIME(handle)
  ↓
PRE_ROUTE
  ↓
Router → validated FAST Decision
  ↓
CapabilityInvoker.invoke(
    decision.capability_id,
    request.input,
    existing context,
    caller-supplied plugin constraint only,
    existing deadline / parent span,
  )
  ↓
ResultEnvelope + route metadata
```

### 结果 metadata

建议增加安全摘要：

```text
execution_mode
route_type
route_reason_code
router_id
capability_id
```

Provider identity 继续由 Provider observability 提供，不由 Router metadata 伪造。

### 完成标准

- AUTO + explicit target 走 FAST。
- FAST 与原 `invoke()` 得到等价业务结果。
- 同一次 handle 只有一个 REQUEST span。
- Deadline 从 handle 传播到 CapabilityInvoker。
- Router 不能创建 plugin/provider pin。
- `invoke()` 原有测试全部通过。

---

## Step 5 — Planner SPI / Registry / Static / Hybrid

### 目标

建立不依赖 LLM 的 Planner 稳定边界，为 Composition Root 的服务端 Planner 选择提供可验证落点。

### PlannerRegistry

第一版是 Composition Root 内的本地只读映射：

```text
register during build
get(planner_id)
list planner IDs
duplicate ID rejected
```

运行中不支持插件动态注册 Planner。

### StaticPlanner

最小实现接受由调用方配置的 plan factory / template mapping：

```text
request route key → ExecutionPlan factory
```

它用于：

- 确定性任务；
- HybridPlanner 的 primary；
- 不调用模型的 contract / integration tests。

它不是 WorkflowSPI，不引入版本化 Workflow Catalog。

### HybridPlanner

```text
primary planner
    ├── valid plan → return
    ├── NOT_APPLICABLE → fallback planner
    └── any other failure → propagate
```

所有 delegate 输出仍由 HybridPlanner 最后再做一次 PlanValidator 校验。

### 完成标准

- Composition Root 可以验证 default_planner_id，Router 不接收 PlannerRegistry。
- StaticPlanner 结果必须通过 PlanValidator。
- HybridPlanner primary 命中时 fallback 零调用。
- primary NOT_APPLICABLE 时 fallback 恰好调用一次。
- primary invalid / denied / timeout 时禁止 fallback。

---

## Step 6 — LLMRouter

### 目标

使用 Stage 3A ModelGateway，为未被 RuleRouter 解决的请求生成结构化 RouteDecision。

### 模型调用

```python
GenerateRequest(
    model=route_model_capability_id,
    messages=(...),
    response_format=ModelResponseFormat.JSON,
    response_schema=RouteDecision.model_json_schema(),
    temperature=0.0,
    metadata={"purpose": "route", "prompt_version": "route-v1"},
)
```

`model` 是逻辑 Model Capability ID，不是 provider_id。

### Prompt 输入

只包含：

```text
RequestSummary
effective requested mode
allowed modes
capability-only Catalog snapshot
allowed capability IDs
RouteDecision JSON schema
```

不包含：

```text
ProviderDescriptor
provider priority / health
plugin instance
Policy implementation details
StateStore data
Trace baggage
```

### 结果处理

```text
ModelGateway GenerateResult
  ↓
JSON output check
  ↓
RouteDecision parse
  ↓
RouteDecisionValidator
  ↓
validated decision or RoutingError
```

第一版不做 LLM Route repair loop。结构非法时直接失败；Provider retry / fallback 仍由 ModelGateway 负责。

### 完成标准

- LLMRouter 只依赖 ModelGateway，不依赖厂商 SDK。
- 模型输出 provider_id / plugin_id 因 schema extra field 被拒绝。
- 模型输出 planner_id 因 schema extra field 被拒绝；Planner 选择属于服务端控制面。
- 不存在的 capability 被 Validator 拒绝。
- 固定 PLAN 不能被模型改成 FAST。
- ModelGateway failure 被映射为安全 Route error。
- LLMRouter 路由期间业务 Capability 调用次数为零。

---

## Step 7 — PlanDraft / LLMPlanner

### 目标

让模型从 Goal + Catalog 生成第一版结构化计划，并保证模型没有执行权。

### 模型调用

```python
GenerateRequest(
    model=planner_model_capability_id,
    messages=(...),
    response_format=ModelResponseFormat.JSON,
    response_schema=PlanDraft.model_json_schema(),
    temperature=0.0,
    metadata={"purpose": "plan", "prompt_version": "planner-v1"},
)
```

### Generation pipeline

```text
PlanningContext
  ↓
safe prompt projection
  ↓
ModelGateway
  ↓
PlanDraft parse
  ↓
assign plan_id / revision / harness metadata
  ↓
planning limits validation
  ↓
PlanValidator
  ↓
ExecutionPlan
```

### 额外 Planning Guard

PlanValidator 之外还要校验：

```text
node count <= max_plan_nodes
all capability IDs in allowed scope
plan deadline does not exceed Request deadline
metadata has no reserved field injection
plan_id / revision are Harness-owned
```

### 完成标准

- valid first response 返回 ExecutionPlan。
- 模型不能注入 plan_id / revision。
- Catalog 中不暴露 provider identity。
- LLMPlanner 不持有 CapabilityInvoker。
- LLMPlanner 不执行任何 Capability。
- PlanValidator 对生成计划真实生效。

---

## Step 8 — Bounded Plan Repair

### 目标

允许模型根据结构化错误修复 Plan，但严格限制次数、Deadline 和输入范围。

### Attempt 状态机

```text
attempt 1: initial
  ├── valid → completed
  └── invalid
        ↓
attempt 2: repair
  ├── valid → completed
  └── invalid
        ↓
attempt 3: repair
  ├── valid → completed
  └── invalid → REPAIR_EXHAUSTED
```

### Repair 输入

```text
same RequestSummary
same Catalog snapshot
same PlanningConstraints
previous PlanDraft JSON（bounded）
parse error type / location（sanitized）
PlanValidationIssue(code, node_id, field, reference)
PlanDraft schema
```

不把 Python traceback、异常对象或隐藏推理传回模型。

### Budget 规则

- `max_plan_attempts` 包含 initial attempt；
- 每次调用前检查 Invocation deadline；
- ModelGateway timeout 不自动增加 plan attempt；
- ModelGateway 完成一次失败 generation 才消耗一个 plan attempt；
- 只有成功 generation 的结构化输出校验失败才进入 repair；ModelGateway failure 直接保留
  `PLANNER_MODEL_FAILED`；
- Policy 上限只能收紧默认值；
- repair exhausted 时零节点执行、零 Plan checkpoint。

实现通过 `PlanningAttemptObserver` 输出 `PlanningAttempt` 安全摘要；Observer 不接收 raw prompt、
raw output、异常 message/input 或 Chain-of-Thought。上一轮 JSON 在进入 repair prompt 前必须限制
深度、集合大小、字符串长度和总值数量。

### 完成标准

- invalid → repair → valid 精确执行两次 generation。
- repair 使用同一 Catalog snapshot。
- repair exhausted 返回固定错误码。
- max attempts 为 1 时不发 repair 请求。
- Deadline 到期后不再调用模型。
- 所有 attempt 均有可观测摘要，不保存 Chain-of-Thought。

---

## Step 9 — `handle()` PLAN Path / Shared Lifecycle

### 目标

把经过验证的 Planner 输出接回 Stage 2 ExecutionEngine，并保持一条请求生命周期。

### 流程

```text
validated PLAN RouteDecision
  ↓
RequestCoordinator 根据 default_planner_id + PRE_ROUTE allowed_planner_ids 选择 Planner
  ↓
PlannerRegistry.get(default_planner_id)
  ↓
Planner.plan(PlanningContext)
  ↓
validated ExecutionPlan
  ↓
ExecutionEngine.execute_with_context(
    normalized request,
    plan,
    existing invocation context,
    parent=planner/runtime span,
  )
  ↓
PRE_PLAN Policy
  ↓
Scheduler / Checkpoint / Resume
```

### 双重验证

LLMPlanner 完成后验证一次；ExecutionEngine 入口继续验证一次。

这是有意设计：

- Planner validation 保证不会返回非法 Plan；
- ExecutionEngine validation 保证它不信任任何调用方或 Planner；
- 两次之间 Catalog / Policy 变化时执行边界仍 fail-closed。

### Resume

3B 不新增新的 Plan resume API：

```python
await app.resume_plan(plan_id)
```

继续恢复 Stage 2 保存的 ExecutionPlan、Context 和 Node State。

Route / Planning 发生在首次创建 Plan 之前，Plan 已 checkpoint 后 resume 不重新 Route，也不重新调用 Planner。

### 完成标准

- 首次 PLAN 执行 Route / Planner 各运行一次。
- WAITING 后 resume 不重新路由 / 规划。
- Crash 后 resume 不重新调用 LLMPlanner。
- 生成 Plan 的 PRE_PLAN / PRE_EXECUTE Policy 均生效。
- 一个 handle 只有一个 REQUEST trace_id。
- Stage 2 checkpoint 格式不因 RouteDecision 被强制修改。

RouteDecision / PlanningAttempt 第一版通过 Trace / Events 审计，不进入 PlanExecutionRecord；如果未来需要从“规划中 crash”恢复，应单独设计 Request-level orchestration checkpoint，不能混入 Node checkpoint。

---

## Step 10 — Route / Planner Observability

### Trace

ROUTE Span 至少记录：

```text
router_id
requested_mode
effective_mode
route_type
decision_source
reason_code
confidence
catalog_snapshot_hash
request_summary_hash
```

PLANNER Span 至少记录：

```text
planner_id
prompt_version
attempt_count
plan_id
plan_revision
node_count
validation_result
catalog_snapshot_hash
```

MODEL 子 Span 已由 ModelGateway 提供：

```text
model capability
provider_id
retry / fallback
usage
finish reason
```

### Events

建议属性：

```text
route.decided:
    router_id, mode, route_type, reason_code, confidence

mode.selected:
    requested_mode, selected_mode, source

route.failed:
    router_id, error_code

planner.started:
    planner_id, prompt_version, max_attempts

planner.repairing:
    planner_id, attempt, validation_codes

planner.completed:
    planner_id, attempt_count, plan_id, node_count

planner.failed:
    planner_id, attempt_count, error_code, validation_codes
```

### 安全要求

Event / Trace 禁止记录：

```text
raw user content
raw prompt
raw model response
hidden chain-of-thought
credentials
full policy attributes
```

### 完成标准

- RuleRouter 与 LLMRouter 都产生 ROUTE Span。
- LLMRouter MODEL Span 是 ROUTE 子孙。
- LLMPlanner 每次 generation 都有 MODEL Span。
- repair 用 Event 表示，不为每个瞬时状态新增 SpanType。
- handle 结果 trace_id 能关联到 Route、Planner、Plan 和 Provider。

---

## Step 11 — Stage 3B Acceptance

### 目标

建立 Stage 1 / 2 / 3A / 3B 的统一回归 Gate。

### 新增测试目录

```text
tests/stage3b/
├── README.md
├── support.py
├── test_execution_mode.py
├── test_rule_routing.py
├── test_llm_routing.py
├── test_llm_planning.py
├── test_handle_lifecycle.py
├── test_policy.py
└── test_regression_gate.py
```

### 必测场景

```text
AUTO explicit target → FAST
forced FAST / PLAN
EXPLORE / HYBRID fail-closed
PRE_ROUTE deny / forced mode / scope
RuleRouter no match
LLMRouter valid / invalid / model failure
LLMPlanner valid first attempt
invalid → repair → valid
repair exhausted
unknown capability / cycle / oversized plan
HybridPlanner deterministic hit / not-applicable fallback
PLAN execution / WAITING / resume
single Context / deadline / trace propagation
Stage 1 direct invocation regression
Stage 2 execution / resume regression
Stage 3A provider fallback / model gateway regression
```

### 完成标准

- Stage 3B unit / contract / integration tests 通过。
- Stage 1 / 2 / 3A 全量回归通过。
- Ruff check / format 通过。
- `git diff --check` 通过。
- Router / Planner 零裸 Provider 调用。
- 非法 Route / Plan 下业务 Capability 调用次数为零。

### 实施结果（2026-08-28）

已新增：

```text
tests/stage3b/
├── README.md
├── support.py
├── test_execution_mode.py
├── test_rule_routing.py
├── test_llm_routing.py
├── test_llm_planning.py
├── test_handle_lifecycle.py
├── test_policy.py
└── test_regression_gate.py
```

验收覆盖本文全部必测场景，并额外锁定：

- Policy forced mode 的错误码优先级；
- MODEL Span 不复制 Provider/模型原始失败消息；
- repair exhausted 时 StateStore create/save 与业务 Capability 调用均为零；
- SQLite WAITING 跨 Application 恢复不重新调用 LLMRouter/LLMPlanner；
- Router/Planner 源码不依赖 ExecutionEngine、CapabilityInvoker、StateStore 或 Provider SPI。

最终全模块回归结果：`385 passed, 103 subtests passed`。唯一 warning 是 Stage 2 既有
`TestPlugin` pytest collection 提示，不影响执行结果。

---

# 9. 关键运行语义

## 9.1 AUTO 不等于“总是调用 LLM”

推荐 deterministic-first：

```text
AUTO + explicit target
    → RuleRouter → FAST

AUTO + deterministic rule
    → RuleRouter → FAST / PLAN

AUTO + ambiguous
    → LLMRouter
```

这样可以降低延迟、成本和不确定性。

## 9.2 FAST 仍面向 Capability

Router 返回：

```text
capability_id
```

实际 Provider 继续由 Stage 3A 决定：

```text
CapabilityInvoker
  ↓
ProviderSelector
  ↓
ProviderExecutionCoordinator
```

Router 不得通过 metadata 偷渡 provider pin。

## 9.3 PLAN 只在验证完成后开始执行

```text
model output
    ≠ Execution authorization

parsed PlanDraft
    ≠ executable plan

validated ExecutionPlan
    + PRE_PLAN allow
    = may enter Scheduler
```

## 9.4 规划期间 crash 的语义

3B 第一版不 checkpoint Router / Planner 中间态。

如果进程在 Plan 首次持久化前 crash：

```text
调用方重试 handle
    → 可能重新 Route / Plan
```

安全依据：此时没有业务 Capability 被执行。

一旦 ExecutionEngine 创建 Plan checkpoint：

```text
resume_plan(plan_id)
    → 不重新 Route / Plan
```

如果未来规划本身具有昂贵成本或要求完全可重放，再在 3D Eval / 后续阶段增加 Request-level decision record。

## 9.5 Planner 和 Provider fallback 不混淆

```text
Model Provider fallback
    = 同一次 GenerateRequest 从 Model Provider A 切到 B

Plan repair
    = 新的一次 GenerateRequest，要求修复结构化计划

HybridPlanner fallback
    = primary 明确 NOT_APPLICABLE 后切换 Planner strategy
```

三者必须使用不同 Trace / Event 语义。

---

# 10. 推荐模块实施顺序

```text
1. harness-contracts
        ↓
2. harness-routing [NEW]
        ↓
3. harness-policy
        ↓
4. harness-planning
        ↓
5. harness-bootstrap
        ↓
6. harness-execution context-aware refactor
        ↓
7. harness-trace / harness-events
        ↓
8. tests/stage3b
        ↓
9. README / design docs
```

风险最高的修改：

```text
harness-bootstrap RequestCoordinator
harness-execution ExecutionEngine context-aware entry
harness-planning bounded repair loop
```

这三处需要重点验证 Deadline、Trace、错误归一化与零重复执行。

---

# 11. 推荐提交拆分

## Commit 1 — Routing Contracts

```text
ExecutionMode
RouteType / RouteSource / RouteDecision
RequestOptions.execution_mode
Route / Planner errors
```

## Commit 2 — Routing Foundation

```text
harness-routing
Router SPI
RequestSummary / Projector
RuleRouter
RouteDecisionValidator
```

## Commit 3 — PRE_ROUTE Policy

```text
PolicyPhase.PRE_ROUTE
PolicyContext validation
typed constraints / reducer
policy tests
```

## Commit 4 — Handle FAST

```text
RequestCoordinator
HarnessApplication.handle
single-context FAST dispatch
compatibility tests
```

## Commit 5 — Planner Foundation

```text
Planner SPI
PlannerRegistry
StaticPlanner
HybridPlanner
```

## Commit 6 — LLM Routing

```text
LLMRouter
ModelGateway integration
structured decision tests
```

## Commit 7 — LLM Planning / Repair

```text
PlanDraft
LLMPlanner
Planning guards
bounded repair
```

## Commit 8 — Handle PLAN / Shared Lifecycle

```text
ExecutionEngine context-aware entry
PLAN dispatch
WAITING / resume integration
```

## Commit 9 — Observability

```text
ROUTE trace
route / planner events
safe attributes
```

## Commit 10 — 3B Acceptance / Docs

```text
tests/stage3b
full regression
module README updates
Stage 3 ADR update
```

---

# 12. 最终验收场景

## 场景 A：确定性 AUTO → FAST

输入：

```text
Request.options.execution_mode = AUTO
Request.target.capability = calculator.evaluate/v1
```

预期：

```text
PRE_ROUTE allow
RuleRouter → FAST
LLMRouter.calls == 0
LLMPlanner.calls == 0
CapabilityInvoker.calls == 1
Result SUCCESS
```

验证一条完整 Route → Provider trace。

## 场景 B：AUTO → LLM Route → PLAN

输入：

```text
“查询两个数据源并汇总结果”
target = None
```

预期：

```text
RuleRouter no match
  ↓
LLMRouter → RouteDecision(PLAN)
  ↓
RequestCoordinator → server-selected LLMPlanner(llm-default)
  ↓
LLMPlanner → valid PlanDraft
  ↓
PlanValidator
  ↓
ExecutionEngine
  ↓
SUCCESS
```

验证 Router / Planner 期间业务 Capability 零调用，执行阶段才调用。

## 场景 C：Plan Repair

模型序列：

```text
attempt 1 → unknown capability / invalid edge
attempt 2 → valid plan
```

预期：

```text
planner.started
planner.repairing(attempt=2, validation_codes=...)
planner.completed(attempt_count=2)
ExecutionEngine executes once
```

禁止第一次非法 Plan 产生 checkpoint 或节点调用。

## 场景 D：Repair Exhausted

```text
attempt 1 invalid
attempt 2 invalid
attempt 3 invalid
```

预期：

```text
HARNESS.PLANNER.REPAIR_EXHAUSTED
CapabilityInvoker.calls == 0
StateStore.create.calls == 0
planner.failed event emitted
```

## 场景 E：Policy 强制模式

```text
Request = AUTO
PRE_ROUTE constraint force_mode=PLAN
```

Router 最终只能返回 PLAN。若返回 FAST：

```text
HARNESS.ROUTE.MODE_NOT_ALLOWED
```

不得执行 Capability。

## 场景 F：3C 模式 Fail Closed

```text
Request.execution_mode = EXPLORE
Request.execution_mode = HYBRID
```

均返回：

```text
HARNESS.ROUTE.MODE_NOT_AVAILABLE
```

Router、Planner、Capability 调用次数符合确定性预期，且不能静默转 PLAN。

## 场景 G：WAITING / Resume 不重新规划

LLMPlanner 生成包含 Approval Node 或异步 Capability 的 Plan：

```text
handle
  ↓
PLAN WAITING
  ↓
resolve_approval / complete_async_node / resume_plan
  ↓
continue
```

验证：

```text
LLMRouter.calls == 1
LLMPlanner.calls == 1
resume 后保持不变
```

## 场景 H：HybridPlanner Deterministic First

```text
StaticPlanner applicable
    → LLMPlanner.calls == 0

StaticPlanner NOT_APPLICABLE
    → LLMPlanner.calls == 1
```

StaticPlanner 返回非法 Plan 时：

```text
fail
LLMPlanner.calls == 0
```

---

# 13. 3B 完成定义

以下条件全部满足后才能进入 Stage 3C：

- ExecutionMode 已成为 RequestOptions 的稳定字段。
- 旧 Request 数据默认兼容 AUTO。
- `handle()` 支持 AUTO / FAST / PLAN。
- EXPLORE / HYBRID 在 3B 明确 fail-closed。
- RuleRouter deterministic-first。
- LLMRouter 只返回 RouteDecision。
- Router 不接触 Provider instance / CapabilityInvoker。
- RouteDecision 在 Harness dispatch 前经过独立 Validator。
- PRE_ROUTE 可以 deny、force mode 和限制 route scope。
- PRE_ROUTE REQUIRE_APPROVAL 不产生不可恢复的伪 waiting。
- Planner SPI 与 PlannerRegistry 边界稳定。
- HybridPlanner 只在 NOT_APPLICABLE 时 fallback。
- LLMPlanner 只输出经过验证的 ExecutionPlan。
- 模型不能控制 plan_id / revision / provider identity。
- Plan repair 有 attempts / deadline / scope 上限。
- 非法或 repair exhausted Plan 零业务执行。
- `handle()` 只创建一个 Context、Deadline 和 REQUEST trace。
- 首次 Plan checkpoint 后 resume 不重新 Route / Plan。
- Route / Planner / Model / Plan / Provider Trace 可关联。
- 不保存隐藏 Chain-of-Thought、原始 Prompt 或完整模型响应。
- Stage 1 / 2 / 3A / 3B 全量测试通过。

---

# 14. 已确认并落地的决议

## RESOLVED-3B-1：3B 支持模式范围

已实现：

```text
AUTO / FAST / PLAN = available
EXPLORE / HYBRID   = contract only + fail-closed
```

这是与当前代码成熟度最一致的拆分，也避免把 3C 的 checkpoint / scope / recursion 问题提前混入 3B。

## RESOLVED-3B-2：PRE_ROUTE Approval

已采用 fail-closed，不新增 Request-level waiting state。

如果业务确认路由前必须人工审批，则需要扩大 3B 范围，新增：

```text
OrchestrationExecutionRecord
Route checkpoint
handle resume API
approval continuation
```

这会显著增加 3B 复杂度，不建议现在引入。

## RESOLVED-3B-3：默认 Router / Planner

已实现：

```text
default Router = RuleRouter without model fallback
default Planner = none
```

因此：

- 旧 `build_harness()` 不依赖 ModelProvider；
- 明确 target 的 handle 可直接工作；
- 模糊 AUTO / PLAN 在未配置 Planner 时返回明确错误；
- 需要 LLM 行为的应用显式注入 LLMRouter / LLMPlanner。

另一方案是默认构造指向 `model.generate/v1` 的 LLMPlanner，但这会让基础 Harness 在没有模型 Provider 时隐式失败。本文不推荐。

## RESOLVED-3B-4：LLM Route 是否 repair

已实现：

```text
3B 不做 Route repair
Plan 做 bounded repair
```

RouteDecision 很小，非法输出直接 fail-closed 更容易观察；未来可根据真实失败率决定是否增加一次 route repair。

## RESOLVED-3B-5：规划中间态是否 checkpoint

已实现为 3B 不 checkpoint planning attempt。

原因：

- Plan 创建前尚未执行业务副作用；
- Stage 2 StateStore 语义是 Execution truth，不宜混入未完成决策草稿；
- Trace / Events 已足够满足第一版审计。

需要精确规划重放时，在 3D 设计独立 DecisionRecord / EvalCase。

---

# 15. 一句话原则

> **Stage 3B 让模型决定“走哪条受控路径、计划长什么样”，但是否允许、是否合法、何时执行，始终由 Harness 决定。**

实现优先级：

```text
Deterministic First
        ↓
Contract Validation
        ↓
Policy Boundary
        ↓
Bounded Model Use
        ↓
Reliable Plan Execution
        ↓
Observability
```
