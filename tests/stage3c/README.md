# Stage 3C Acceptance Gate

当前目录按 Stage 3C 实施步骤逐步扩展。第一步覆盖 Plan identity trust boundary：

- `handle()` 对可复用 Static template 每次物化新的 `plan_id`；
- legacy 自定义 Planner 重复返回同一 `ExecutionPlan` candidate 时仍生成不同身份；
- `revision` 在 fresh execution 中固定从 1 开始，resume 保持原身份；
- `execute_plan()` 保持调用方提供的具体执行身份，并拒绝重复创建；
- `PlanTemplate` 不接受 `plan_id`、`revision` 或 runtime metadata。

第二步覆盖 Strict Structured Output Foundation：

- REQUIRED 不支持时和 Schema preparation 失败时 Provider generation 为零，不允许静默降级；
- 完整本地 JSON Schema 校验覆盖 nested type、enum 和 `additionalProperties`；
- refusal、truncation、content filter 在 Draft parser 前归一化；
- 首 Provider 消耗后失败、fallback 成功时聚合全部 token/cost accounting；
- reservation 冻结所有 retry/fallback slot 的 input/output token 与 normalized cost 上界；
- 缺少 receipt、STARTED CAS/ticket 失配时 outbound 为零，terminal CAS 失败后 generation orphaned；
- LLMPlanner-v2 的 `PlanNodeDraft` 拒绝身份、retry、idempotency、timeout 和 metadata 注入；
- legacy JSON best-effort 路径保持兼容。

测试只使用本地确定性 Provider、内存 StateStore，不访问网络。
