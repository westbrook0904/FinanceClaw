# harness-bootstrap

`harness-bootstrap` 是唯一 Composition Root，负责组装 Harness 的具体基础设施实例，
并协调本地插件与 Application 生命周期；它不承担业务执行逻辑。

## 默认组装

```text
build_harness()
├── InMemoryCapabilityRegistry
│   └── RegistryCapabilityCatalog
├── PolicyEngine(AllowAllPolicy)
├── InMemoryTracer
├── DefaultInvocationContextFactory
│   └── InvocationLifecycle
├── CapabilityInvoker
│   └── ProviderExecutionCoordinator
├── SafeRequestProjector / RuleRouter / RouteDecisionValidator
├── RequestCoordinator（单 Context / Deadline / Request Trace 的 FAST 调度）
├── PlannerRegistry（构造期只读 Planner 映射）
├── ModelGateway（共享 Registry / Selector / Coordinator / Tracer / Events）
├── PlanValidator
├── InMemoryStateStore
├── InMemoryEventBus
├── BasicScheduler
├── ExecutionEngine
├── HarnessRuntime
├── LocalPluginProvider / LocalPluginLoader
└── HarnessApplication
```

`build_harness()` 只创建和连接对象，不发现或初始化插件，也不创建数据库文件。可注入
Registry、PolicyEngine/Policies、Tracer、ProviderSelector、ContextFactory、CapabilityCatalog、
PlanValidator、StateStore、EventPublisher、Router、Planners、Default Planner、
RequestProjector 或 LocalPluginProvider。

自定义组件必须共享一致边界，例如自定义 Catalog 与 PlanValidator.catalog 必须相同；
`policies` 与 `policy_engine`、`plugins` 与 `plugin_provider` 不能同时配置。

## HarnessApplication API

- `start()`：发现、初始化并注册插件。
- `handle(request, mode=None)`：推荐的统一入口；当前完成 PRE_ROUTE、确定性路由与 FAST
  Capability 调用。
- `invoke(request)`：Direct Invocation。
- `model_gateway`：供未来 Router/Planner/Explorer 使用的模型生成入口。
- `execute_plan(request, plan)`：验证并执行 Plan。
- `resume_plan(plan_id)`：从 StateStore 恢复并继续相同 Plan。
- `resolve_approval(plan_id, decision)`：保存审批决定并继续。
- `complete_async_node(plan_id, node_id, terminal_result)`：保存异步节点终态并继续。
- `cancel_plan(plan_id, reason)`：取消当前进程内活动 Plan。
- `shutdown()`：注销 Capability 并关闭全部插件。
- `components` 及各组件 property：访问当前组装使用的只读组件引用。
- `HarnessComponents`：组装完成后的 frozen dataclass 快照。

推荐使用异步上下文管理器：

```python
async with build_harness() as app:
    result = await app.handle(request)
```

## 生命周期

```text
CREATED
  │ start()
  ▼
STARTED
  │ shutdown()
  ▼
STOPPED
```

- STARTED 时重复 `start()` 幂等。
- 重复 `shutdown()` 幂等。
- STOPPED Application 不能重启，应重新调用 `build_harness()`。
- 启动批次失败由 LocalPluginLoader 回滚，Application 保持 CREATED。
- handle/invoke/execute/resume/approval/async completion/cancel 入口只允许在 STARTED 状态，
  否则抛出 `BootstrapStateError`。

## 插件发现

默认扫描 `financeclaw.plugins` entry point。可以用 `plugins=(...)` 显式传入插件，
通常同时设置 `entry_point_group=None`；`plugins` 与自定义 `plugin_provider`
互斥。

## SQLite Resume

默认 StateStore 是内存实现。需要跨 Application/进程恢复时显式注入文件数据库：

```python
from harness_bootstrap import build_harness
from harness_state import SQLiteStateStore

app = build_harness(state_store=SQLiteStateStore("financeclaw-state.db"))
await app.start()
result = await app.resume_plan("plan-123")
```

## Approval 与 Async completion

显式或 Policy Approval 返回 ACCEPTED 后，使用 Continuation 中的 `approval_id`：

```python
from harness_contracts import ApprovalDecision, ApprovalDecisionType

decision = ApprovalDecision(
    approval_id=waiting.continuation.approval_id,
    decision=ApprovalDecisionType.APPROVED,
    decided_by="reviewer-42",
)
result = await app.resolve_approval(plan.plan_id, decision)
```

异步 Capability 返回 `ACCEPTED + job_ref` 后提交终态：

```python
from harness_contracts import ResultEnvelope, ResultOutput

result = await app.complete_async_node(
    plan.plan_id,
    waiting.continuation.node_id,
    ResultEnvelope.success(ResultOutput(type="json", data={"value": 42})),
)
```

两种 ingress 都先持久化状态，再复用 `resume_plan` 的恢复状态机。

## Policy / Trace / Events

默认 AllowAllPolicy 同时参与 PRE_ROUTE/PRE_PLAN/PRE_EXECUTE。可配置
`RequireApprovalPolicy`；批准后的节点携带结构化 ApprovalGrant 再次经过 PRE_EXECUTE。

默认 InMemoryEventBus 不产生磁盘或网络副作用，并可通过
`app.event_publisher` 访问；Tracer、StateStore 和 EventPublisher 的生命周期均由
Composition Root 决定。

## 依赖边界与当前范围

Bootstrap 可以依赖全部 Harness 基础设施，其他核心模块不得反向依赖 Bootstrap。
基础设施类不实现全局单例，实例数量与共享关系由组装决定。

ModelGateway 已组装但不经 CapabilityInvoker；当前 `handle()` 只支持 FAST，PLAN 调度、
LLM Planner、Workflow SPI、Remote Plugin、MCP、分布式调度和 HTTP 执行 API 尚未实现。
LLMRouter 已可作为显式 Router 或 RuleRouter fallback 注入，默认组装仍保持无模型的
RuleRouter。`app.planner_registry` 已可用于本地 Static/Hybrid Planner 配置和
RouteDecision planner ID 校验，但在 PLAN shared lifecycle 接入前不会调用 Planner。

## 测试

```bash
.venv/bin/python -m pytest harness-bootstrap/tests -v
```
