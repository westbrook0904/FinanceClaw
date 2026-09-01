# Agent Foundation / Stage 3C 前置步骤 Acceptance Gate

当前目录按 Stage 3C 实施步骤逐步扩展。第一步覆盖 Plan identity trust boundary：

当前后续 Gate 以 Agent Foundation 一期路线图为准：优先 Context Engineering、Memory 与最小
standalone EXPLORE；HYBRID、PlanPatch、复杂恢复和高阶预算不进入本目录的一期阻断范围。
具体 Contract 与实施顺序以 `.design/FinanceClaw-Agent-Foundation-一期实施说明书.md` 为准；
旧 Stage 3C Agentic Exploration 草案不再作为测试清单来源。

- `handle()` 对可复用 Static template 每次物化新的 `plan_id`；
- legacy 自定义 Planner 重复返回同一 `ExecutionPlan` candidate 时仍生成不同身份；
- `revision` 在 fresh execution 中固定从 1 开始，resume 保持原身份；
- `execute_plan()` 保持调用方提供的具体执行身份，并拒绝重复创建；
- `PlanTemplate` 不接受 `plan_id`、`revision` 或 runtime metadata。

第二步覆盖 Strict Structured Output Foundation：

- REQUIRED 不支持时和 Schema preparation 失败时 Provider generation 为零，不允许静默降级；
- 完整本地 JSON Schema 校验覆盖 nested type、enum 和 `additionalProperties`；
- refusal、truncation、content filter 在 Draft parser 前归一化；
- 首 Provider 消耗后失败、fallback 成功时聚合全部 token usage 遥测；
- reservation 冻结所有允许的 retry/fallback slot，但不承担 token、成本或耗时预算；
- 缺少 receipt、STARTED CAS/ticket 失配时 outbound 为零，terminal CAS 失败后 generation orphaned；
- reservation 绑定 trusted Request、Tenant、Identity 与执行引用，跨授权上下文复用时 outbound 为零；
- 同配置 Provider 实例热替换后 incarnation 变化，旧 reservation 在 outbound 前 orphaned；
- incomplete usage 只降低 telemetry completeness，不改变成功 generation；Draft 2020-12
  primitive-root JSON 可正常通过；
- LLMPlanner-v2 的 `PlanNodeDraft` 拒绝身份、retry、idempotency、timeout 和 metadata 注入；
- legacy JSON best-effort 路径保持兼容。

测试只使用本地确定性 Provider、内存 StateStore，不访问网络。
