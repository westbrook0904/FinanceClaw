# Stage 3 待讨论 ADR 摘要

当前设计已经可以开始做 Stage 3A，但以下 5 个点建议正式拍板：

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
   - 推荐 ModelProvider 有独立 SPI + ModelGateway。
   - 仍需拍板：ModelGateway 是否必须走 CapabilityInvoker，还是复用 Selection / Policy / Trace 形成独立平台边界。

5. **WRITE Fallback**
   - 推荐：只有 `stable idempotency key + provider equivalence_group` 同时满足才允许跨 Provider 自动 fallback。
   - 其他 WRITE fail-closed。

如果只优先讨论一个，我建议先讨论 **第 4 点 ModelProvider / CapabilityInvoker 的关系**，因为它会直接影响 LLMRouter、LLMPlanner 和 ExplorationEngine 的依赖方向。
