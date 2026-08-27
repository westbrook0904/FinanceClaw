# contract-tests

契约测试主要位于：

- `harness-contracts/tests`：Request、Context、Descriptor、ExecutionPlan、Binding、
  Condition、Plan/Node State、Approval、Continuation、ResultEnvelope、错误、冻结和
  JSON round-trip，以及 ProviderDescriptor、Health、Selection、ProviderAttempt 契约。
- `harness-spi/tests`：AgentRequest、ToolRequest、PluginManifest，以及
  Manifest/Provider Descriptor 一致性。
- `harness-planning/tests`：跨模型 DAG、引用和 Capability Catalog 可执行性校验。
- `harness-model/tests`：GenerateRequest/GenerateResult、structured output 和 usage 契约。

这些测试保证模块可以只依赖顶层公共 API 开发，并尽早发现协议字段、序列化、冻结/可变
边界或执行语义的非兼容变化。

```bash
.venv/bin/python -m pytest \
  harness-contracts/tests harness-spi/tests harness-planning/tests \
  harness-model/tests -v
```
