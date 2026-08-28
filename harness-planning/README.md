# harness-planning

`harness-planning` 是 `ExecutionPlan` 的生成策略与执行前确定性校验边界。它不执行
Capability，也不能访问 Provider 实例；Planner 输出统一交给 `PlanValidator` 做结构与
可执行性验证。

## 公共 API

- `Planner`：异步生成标准 `ExecutionPlan` 的无执行权 SPI。
- `PlanningContext` / `PlanningConstraints`：受限 Goal、Capability-only Catalog、大小范围
  与 Deadline 快照。
- `PlannerRegistry`：Composition Root 构造期冻结的本地只读 Planner 映射。
- `StaticPlanner`：按 request route key 选择不可变 Plan 模板或同步/异步 factory。
- `HybridPlanner`：仅在 primary 抛出 `PlannerNotApplicableError` 时调用 fallback。
- `PlanDraft`：模型可生成的受限 DAG 协议，不包含 plan_id、revision 或 Plan metadata。
- `LLMPlanner`：通过 ModelGateway 从 Goal + capability-only Catalog 自主生成 PlanDraft，
  由 Harness 分配计划身份并执行 planning guards 与 PlanValidator。
- `PlanningAttempt` / `PlanningAttemptObserver`：仅输出 attempt 序号、类型、Provider、输出哈希、
  token、结构化 validation codes 和是否安排 repair 的安全观察边界，不保存 prompt、原始输出
  或隐藏推理。`Planner.plan_with_observer()` 支持为单次并发调用附加 observer，不修改共享
  Planner 实例。
- `PlanValidator.validate(plan, executable=True) -> ExecutionPlan`：合法时原样返回；
  存在问题时一次性抛出 `PlanValidationError`。
- `PlanValidator.find_issues(plan, executable=True)`：返回顺序稳定、可序列化的全部
  `PlanValidationIssue`，适合 API/UI 展示。
- `PlanValidationCode`：稳定的问题分类枚举。
- `PlanValidationError.issues`：聚合后的结构化问题快照。

注入 `CapabilityCatalog` 后，默认还会校验 Capability 是否存在、Descriptor 是否与
Node 类型兼容；`executable=False` 只进行结构校验。

## 校验范围

`PlanValidator` 覆盖：

- Plan ID、revision、非空节点集合、Deadline 与阶段二 Plan FailurePolicy；
- Node kind、Capability/Approval 字段互斥、timeout、retry 与 node FailurePolicy；
- Edge 端点、自环、root 与 DAG cycle；
- Input/Output Binding 的 JSON Pointer、引用存在性和上游可用性；
- Condition 结构、引用存在性和可用性；
- Capability Catalog 中的可执行性。

校验是无副作用的，不会调用 Registry Provider、Policy 或 Scheduler。

## Planner fallback 语义

```text
primary valid plan      → validate → return，fallback 零调用
primary NOT_APPLICABLE  → fallback 一次 → validate → return
primary invalid/denied/timeout/other failure → 原错误传播，禁止 fallback
```

StaticPlanner、HybridPlanner 和 LLMPlanner 都在返回边界调用 PlanValidator。PlannerRegistry
只允许在构造时传入 Planner，运行中没有 `register()`，也不是插件 Registry 或 Workflow
Catalog。

## LLMPlanner 安全边界

LLMPlanner 的模型只产生 PlanDraft 中的节点、边、绑定与预算。`plan_id`、`revision=1` 和
`planner_id/prompt_version/request_id` metadata 均由 Harness 写入；模型注入这些字段、引用
超出 Catalog/Policy/Planner 交集的 Capability、超过节点上限、扩大 Request Deadline 或写入
保留 metadata 时都会 fail-closed。Catalog 投影不包含 Provider、Plugin 或 Descriptor metadata，
规划期间不会调用任何业务 Capability。

## Bounded Plan Repair

`PlanningConstraints.max_plan_attempts` 包含首次 generation，默认最多 3 次。只有模型已成功
返回、但 JSON/PlanDraft 解析、planning guard 或 PlanValidator 校验失败时才进入 repair；
ModelGateway failure、Harness identity failure 与 Deadline 到期直接保留各自错误语义。

Repair 始终复用同一份 Goal、Capability Catalog、允许范围、Deadline 和 PlanDraft schema，
并额外携带有界的上一轮 JSON、无异常 message/input 的 parse type/location，以及不含 message
的 PlanValidation issue。上一轮 JSON 限制深度、集合大小、字符串长度和总值数量。每次调用前
重新检查 Deadline；达到上限后统一返回 `HARNESS.PLANNER.REPAIR_EXHAUSTED`。整个循环不创建
Plan checkpoint，也不调用 Capability。

Coordinator 把 `repair_scheduled` 映射成 `planner.repairing` Trace/Execution Event；每次模型
generation 仍由 ModelGateway 创建独立 MODEL span，不为 attempt 增加新的 SpanType。

## 依赖边界与当前范围

本模块依赖 `harness-contracts`、`harness-routing` 的安全 RequestSummary 和只读
`CapabilityCatalog`。Catalog 只返回
`CapabilityDescriptor`，即使 Registry 中同一 Capability 存在多个 Provider 也只暴露
一条 capability-only 记录，不会泄露 Provider 身份或实例。

当前已实现 Static/Hybrid Planner Foundation、PlanDraft、LLMPlanner、bounded Plan Repair 和
`handle()` PLAN dispatch。RequestCoordinator 负责服务端 Planner 选择和执行前再次验证；
WAITING / resume 使用 ExecutionEngine 已持久化的 Plan，不重新调用 Planner。运行时 Plan Patch
属于后续步骤。

## 测试

```bash
.venv/bin/python -m pytest harness-planning/tests -v
```
