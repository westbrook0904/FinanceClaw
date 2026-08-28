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

StaticPlanner 和 HybridPlanner 都在返回边界调用 PlanValidator。PlannerRegistry 只允许在
构造时传入 Planner，运行中没有 `register()`，也不是插件 Registry 或 Workflow Catalog。

## 依赖边界与当前范围

本模块依赖 `harness-contracts`、`harness-routing` 的安全 RequestSummary 和只读
`CapabilityCatalog`。Catalog 只返回
`CapabilityDescriptor`，即使 Registry 中同一 Capability 存在多个 Provider 也只暴露
一条 capability-only 记录，不会泄露 Provider 身份或实例。

当前已实现不依赖模型的 Static/Hybrid Planner Foundation；LLMPlanner、动态 Plan Repair、
`handle()` PLAN dispatch 和运行时 Plan Patch 属于后续步骤。

## 测试

```bash
.venv/bin/python -m pytest harness-planning/tests -v
```
