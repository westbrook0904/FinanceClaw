# harness-spi

业务插件实现的最小接口：`AgentSPI`、`ToolSPI`、`PluginSPI` 及其请求协议。插件只依赖
`harness-contracts` 与 `harness-spi`，不知道 LangChain、LangGraph、Registry、Policy 或
Provider selection 的内部实现。

LLM 不再伪装成 Capability；模型由专门的 LangChain ModelRuntime 管理。
