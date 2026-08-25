# contract-tests

契约测试主要位于：

- `harness-contracts/tests`：Request、Context、Descriptor、ResultEnvelope、错误码、冻结和 JSON round-trip。
- `harness-spi/tests`：AgentRequest、ToolRequest、PluginManifest 及 Manifest/Provider 一致性。

这些测试确保其他模块可以只依赖顶层公共 API 开发，并尽早发现协议字段或执行语义的非兼容变化。
