# harness-planning

`harness-planning` 负责生成和验证 `ExecutionPlan`，不执行 Capability，也不能访问
Provider 实例。本阶段首先提供 `PlanValidator`。

`PlanValidator.validate(plan)` 会聚合结构和引用问题后抛出 `PlanValidationError`；
`find_issues(plan)` 可用于在 API/UI 中展示全部结构化问题。注入只读
`CapabilityCatalog` 后还会检查 Capability 是否实际可用；传入 `executable=False`
可只执行结构校验。
