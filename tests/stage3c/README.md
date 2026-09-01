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

Foundation F1 Routing correctness 的 Gate 分布在 `harness-routing/tests`、
`harness-bootstrap/tests/test_llm_router_api.py` 与 `tests/stage3b` 回归套件：

- RoutingPipeline 只在 `HARNESS.ROUTE.NO_MATCH` 时进入模型 fallback；
- 显式 target、固定 PLAN、确定性 rule 与单一 PLAN Policy 均保持零模型调用；
- route-v2 的最小 strict Schema 拒绝模型覆盖 mode/source/route_type 或注入运行身份；
- Prompt 不携带 requested/effective mode，也不投影 Capability metadata；
- LLMRouter 与 LLMPlanner 共用 StructuredGenerationAdapter。

Foundation F2 Context Engineering 与 F3 Memory 的 Gate 分布在 `harness-context/tests`、
`harness-memory/tests`、`harness-contracts/tests`、`harness-policy/tests` 与
`harness-bootstrap/tests`：

- Memory Draft 拒绝可信身份、namespace、sensitivity、retention 与存储 ID；
- Gateway 强制 trusted scope、namespace、evidence、Policy、TTL 和硬大小边界；
- InMemory/SQLite 均验证确定性检索、create-only 幂等/冲突、删除与跨实例持久化；
- 跨请求 write→read 命中 ContextProjection，Memory 指令文本保持 DATA tier；
- 缺少 MemoryProvider 时默认 FAST/PLAN Context 组装不回归。

Foundation F4a Minimal Explore Contracts 的 Gate 分布在 `harness-contracts/tests`、
`harness-agentic/tests`、`harness-planning/tests` 与 `harness-context/tests`：

- Capability completion 默认 UNKNOWN，只有显式 SYNC 且满足类型/side-effect/egress 约束才能进入
  Explore scope；
- Exploration node/spec 与普通 Capability/Approval 字段双向互斥，standalone wrapper 恰好一个
  node、零 edge、唯一 `/output`；
- ProfileSnapshot 随 Plan 保存，nested child state 与 outer node/plan 的身份、状态、结果和时间一致；
- Turn Draft 拒绝运行身份、Provider/Plugin、idempotency 与 Patch 字段，PlanDraft 不暴露
  EXPLORATION kind；
- Profile/scope/proposal/result hash、基础计数和 Action/Observation 引用可重验，损坏统一归类为
  `HARNESS.EXPLORATION.CHECKPOINT_CORRUPT`；
- 默认 Plan 可执行性校验继续返回 `PLAN.EXPLORATION_NOT_AVAILABLE`，F4a 不产生模型或 Capability
  outbound。

测试只使用本地确定性 Provider、内存 StateStore，不访问网络。
