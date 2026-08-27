# Stage 3 ADR 摘要

Stage 3A 已完成。以下决议已在实现中落地，其余项目继续留待 Stage 3B 讨论：

1. **ExecutionMode 的归属**
   - 推荐：`RequestOptions.execution_mode`
   - `handle(..., mode=...)` 仅作为 sugar。

2. **ReAct 的宿主**
   - 推荐 Stage 3：Harness-owned `ExplorationEngine`
   - 暂不把 `CapabilityInvoker` 注入普通 AgentPlugin。
   - 未来如需要高度自定义，再设计 `AgenticAgentSPI + ScopedActionPort`。

3. **WorkflowSPI**
   - 推荐 Stage 3 不做。
   - 固定流程继续使用 StaticPlanner / ExecutionPlan。

4. **ModelProvider 调用边界**
   - 已决议：ModelProvider 使用独立 SPI + ModelGateway。
   - ModelGateway 复用 Registry、Selection/Health、ProviderExecutionCoordinator、Trace/Events，但不经过 CapabilityInvoker，也不把 GenerateRequest 伪装成 Agent/Tool 请求。

5. **WRITE Fallback**
   - 推荐：只有 `stable idempotency key + provider equivalence_group` 同时满足才允许跨 Provider 自动 fallback。
   - 其他 WRITE fail-closed。

Stage 3B 的 Router、LLMPlanner 和 ExplorationEngine 统一依赖 ModelGateway；下一步需要优先冻结 ExecutionMode 和 Planner/Explorer 的职责边界。
