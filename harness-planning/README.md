# harness-planning

`harness-planning` 是 `ExecutionPlan` 进入执行引擎前的确定性校验边界。它不执行
Capability，也不能访问 Provider 实例；当前实现以调用方提供的 Plan 为输入，
由 `PlanValidator` 负责结构与可执行性验证。

## 公共 API

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

## 依赖边界与当前范围

本模块依赖 `harness-contracts` 和只读 `CapabilityCatalog`。Catalog 只返回
`CapabilityDescriptor`，即使 Registry 中同一 Capability 存在多个 Provider 也只暴露
一条 capability-only 记录，不会泄露 Provider 身份或实例。

当前仓库执行显式提交的确定性 `ExecutionPlan`；Rule/LLM Planner、动态 Plan Repair
和运行时 Plan Patch 属于后续决策层，不在本模块当前实现中。

## 测试

```bash
.venv/bin/python -m pytest harness-planning/tests -v
```
