# harness-spi

`harness-spi` 定义业务插件面向 Harness 实现的扩展点，只依赖
`harness-contracts`。阶段二没有扩大插件权限：Agent/Tool 仍只执行单个 Capability，
Plan、Policy、Registry、重试和恢复全部由 Harness 基础设施协调。

## 公共 API

- `Capability`：只统一无副作用的 `descriptor()`。
- `AgentSPI.invoke(AgentRequest, InvocationContext)`。
- `ToolSPI.execute(ToolRequest, InvocationContext)`。
- `PluginSPI`：`manifest()`、`capabilities()`、`initialize()`、
  `shutdown()`。
- `AgentRequest`：保留原始 RequestInput，可附加 instructions。
- `ToolRequest`：包含深度不可变的结构化 arguments。
- `PluginManifest`：声明插件身份、实现版本、SDK 版本和 Capability ID。
- `validate_manifest_capabilities()`：校验 Manifest 与 Provider Descriptor 一致。

## Agent、Tool 与 Plugin

```text
AgentSPI.invoke()  → 自主任务处理
ToolSPI.execute()  → 明确、单步、确定性操作
PluginSPI          → 发现、打包与生命周期
```

一个 Plugin 可以提供一个或多个 Agent/Tool；每个 Provider 都有独立、稳定的
Capability ID。这里刻意不存在万能的 `Plugin.execute()`。

`CapabilityDescriptor.execution_profile` 由 Contracts 定义，插件可用
`side_effect/egress/idempotency` 声明执行语义，供 Scheduler 的安全 Retry/Resume 和
Policy Approval 判断。插件本身不实现这些协调逻辑。

## 生命周期契约

- `manifest()`、`capabilities()` 在插件存活期间稳定且无副作用。
- `initialize()`、`shutdown()` 必须幂等。
- Manifest 中的 Capability ID 不允许为空或重复，并与 Provider Descriptor 完全一致。
- Provider 必须恰好实现 AgentSPI 或 ToolSPI 之一。
- Agent/Tool 最终必须返回 `ResultEnvelope`。

## 依赖边界与当前范围

业务插件只能依赖本模块和 `harness-contracts`，不能依赖 Runtime、Execution、
Registry、Policy、Trace、State 或 Bootstrap。

Remote Agent、MCP/HTTP Provider、流式调用、热升级和 Workflow SPI 不在第二阶段范围内。

## 测试

```bash
.venv/bin/python -m pytest harness-spi/tests -v
```
