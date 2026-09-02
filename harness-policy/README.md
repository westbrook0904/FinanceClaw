# harness-policy

FinanceClaw 自己拥有的治理边界，目前包含：

- `PRE_CONTEXT`：Context 条目进入 Agent/Workflow 投影前；
- `PRE_MEMORY_READ/WRITE/DELETE`：长期记忆访问前；
- `PRE_EXECUTE`：Capability 与具体 Provider 调用前。

Policy 按 `DENY > REQUIRE_APPROVAL > ALLOW` 聚合。它不再负责 LLM 模式选择、PRE_ROUTE 或
PRE_PLAN，也不实现 LangGraph 的控制流。未来 `CapabilityToolAdapter` 和 Workflow 入口必须
调用这些固定边界，不能让框架原生 ToolNode 绕过它们。
