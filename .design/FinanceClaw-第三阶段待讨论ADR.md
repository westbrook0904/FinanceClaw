# Stage 3 ADR 状态摘要

Stage 3A 与 Stage 3B 已完成。原先需要在 3B 前冻结的决议均已落地；当前只保留 Stage 3C/3D
尚未实施的决策边界。

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

## Stage 3C 已冻结、尚未实现

- ReAct 采用 Harness-owned `ExplorationEngine`。
- 普通 AgentPlugin 不获得 `CapabilityInvoker`；模型只提交 `ActionProposal`。
- EXPLORE/HYBRID 实际执行、Explore checkpoint、ScopedActionExecutor、PlanPatchProposal 与
  Plan revision validation 均属于 Stage 3C。

## Stage 3D 待实施

- ConnectorProvider / MemoryProvider。
- Selection / Route / Plan Replay Eval。
- Provider 扩展、对比评测与完整故障注入。
