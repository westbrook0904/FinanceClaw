# harness-runtime

`CapabilityInvoker` 是 FinanceClaw 工具/Agent Capability 的统一受控调用边界：

```text
Capability ID + RequestInput + InvocationContext
  → Registry candidates
  → Selection / Health
  → PRE_EXECUTE Policy
  → AgentSPI / ToolSPI
  → Provider retry / safe fallback
  → ResultEnvelope + Trace + Provider Events
```

`HarnessRuntime.invoke()` 为显式目标请求提供 Direct Invocation。未来 LangChain tool 与
LangGraph workflow node 必须适配到 `CapabilityInvoker`，不能直接持有 Provider 实例。

本模块不负责模型调用、ReAct、DAG、checkpoint 或 Workflow 恢复。模型 retry/fallback 由
LangChain 负责；图节点 retry/checkpoint 由 LangGraph 负责；Capability Provider 的 WRITE
幂等与等价组安全仍由本模块负责。
