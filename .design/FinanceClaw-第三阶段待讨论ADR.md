# Stage 3 ADR 状态摘要

Stage 3A 与 Stage 3B 已完成。Stage 3C 详细设计已经冻结、尚未实施；Stage 3D 仍待设计与实施。
3C 的完整编码基线见
`.design/FinanceClaw-Stage3C-Agentic-Exploration-实施说明书.md`。

## 已决议并实现

1. **ExecutionMode 的归属**
   - 唯一持久化位置为 `RequestOptions.execution_mode`。
   - `handle(..., mode=...)` 仅把 sugar 归一化到不可变 Request 副本。
   - Stage 3B 实际执行 AUTO/FAST/PLAN；EXPLORE/HYBRID fail-closed。

2. **Router / Planner 权限**
   - Router 只产生 `RouteDecision`，不能选择 Provider 或执行 Capability。
   - 模型只产生 `PlanDraft`；Harness 分配 `plan_id/revision`，Planner 返回经过验证的
     `ExecutionPlan`。
   - Planner 由服务端配置与 PRE_ROUTE Policy 选择，不允许模型选择。

3. **Planner 组合与 WorkflowSPI**
   - `HybridPlanner` 仅在 primary 明确 `NOT_APPLICABLE` 时 fallback。
   - Stage 3 不引入 WorkflowSPI；固定流程继续使用 StaticPlanner + ExecutionPlan。

4. **ModelProvider 调用边界**
   - ModelProvider 使用独立 SPI + ModelGateway。
   - ModelGateway 复用 Registry、Selection/Health、ProviderExecutionCoordinator、Trace/Events，
     但不经过 CapabilityInvoker，也不把 GenerateRequest 伪装成 Agent/Tool 请求。

5. **WRITE Fallback**
   - 只有稳定 idempotency key 与相同非空 `equivalence_group` 同时满足时，才允许跨 Provider
     自动 fallback；其他 WRITE fail-closed。

6. **恢复与观察事实**
   - RouteDecision、RequestSummary 和 PlanningAttempt 不写入 Plan checkpoint。
   - 首次合法 Plan checkpoint 后，WAITING/crash resume 不重新 Route/Plan。
   - Trace/Events 只保存安全 ID、hash、计数和验证码，不保存 Prompt、模型响应、Provider
     原始错误消息或隐藏 Chain-of-Thought。

## Stage 3C 设计已冻结、尚未实现

- ReAct 采用 Harness-owned `ExplorationEngine`。
- 普通 AgentPlugin 不获得 `CapabilityInvoker`；模型只提交 identity-free Draft，Harness
  materialize 最终 Proposal。
- EXPLORE/HYBRID 实际执行、PlanExecutionRecord 内的 Exploration child state、
  ScopedActionExecutor、PlanPatchProposal 与 Plan revision validation 均属于 Stage 3C。
- `handle()` 是标准 orchestration API；`invoke()` / `execute_plan()` 是稳定高级 API，
  不删除。
- Routing 默认采用 deterministic-first Pipeline，只有类型化 NOT_APPLICABLE 才进入模型
  fallback。
- 模型只填写未知字段；requested/effective mode 不进入模型 Prompt，Harness 生成最终
  `RouteDecision`。
- Structured Output 采用 Provider-native strict、完整本地 Schema/Pydantic 与业务 Validator
  三层门禁，不支持 strict 时禁止静默降级。
- `plan_id` 是 fresh execution identity；任意 Planner（包含第三方 Planner）输出
  在 Coordinator trust boundary 统一归一化为模板，每次 fresh execution 只由
  PlanMaterializer 物化一次新 ID；Patch 保持同 ID、revision 精确加一。
- standalone EXPLORE 物化为真实单 EXPLORATION 节点 Plan，action 子状态与 Plan 一起
  checkpoint；Approval/Async 恢复定位到 action。
- 任何包含 EXPLORATION 节点的 Plan 都必须设置
  `checkpoint_cas_required=true`，所有 checkpoint / approval / async completion /
  cancel / resume 变更都经 versioned CAS，与是否允许 Patch 无关。
- PlanPatch v1 append-only，必须按 PRE_PATCH → PlanValidator → PRE_PLAN →
  StateStore CAS 顺序通过治理。
- 3C 不自动把成功 Plan 晋升为 Workflow，只记录未来 3D Eval 所需的安全 candidate facts；
  Workflow Catalog/版本化发布仍属于 Stage 4。

## Stage 3D 待实施

- ConnectorProvider / MemoryProvider。
- Selection / Route / Plan Replay Eval。
- Provider 扩展、对比评测与完整故障注入。
