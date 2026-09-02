# plugins

示例业务 Capability 插件。插件通过 `PluginSPI` 暴露 Agent/Tool，并始终由
`CapabilityInvoker` 执行。未来顶层 Agent 会把获准的 Capability 投影为 LangChain tools，
固定 Workflow 会把它们封装为 LangGraph nodes；插件本身无需依赖两个框架。
