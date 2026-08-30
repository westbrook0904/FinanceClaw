# Stage 3C Acceptance Gate

当前目录按 Stage 3C 实施步骤逐步扩展。第一步覆盖 Plan identity trust boundary：

- `handle()` 对可复用 Static template 每次物化新的 `plan_id`；
- legacy 自定义 Planner 重复返回同一 `ExecutionPlan` candidate 时仍生成不同身份；
- `revision` 在 fresh execution 中固定从 1 开始，resume 保持原身份；
- `execute_plan()` 保持调用方提供的具体执行身份，并拒绝重复创建；
- `PlanTemplate` 不接受 `plan_id`、`revision` 或 runtime metadata。

测试只使用本地确定性 Provider、内存 StateStore，不访问网络。
