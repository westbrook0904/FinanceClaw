# harness-spi

`harness-spi` 定义业务插件面向 Harness 实现的扩展点，只依赖 `harness-contracts`。

## 公共 API

- `Capability`：只统一无副作用的 `descriptor()`。
- `AgentSPI`：异步 `invoke(AgentRequest, InvocationContext)`。
- `ToolSPI`：异步 `execute(ToolRequest, InvocationContext)`。
- `PluginSPI`：提供 `manifest()`、`capabilities()`、`initialize()` 和 `shutdown()`。
- `AgentRequest`：保留原始 `RequestInput`，可附加 Agent instructions。
- `ToolRequest`：包含深度不可变的结构化 `arguments`。
- `PluginManifest`：声明插件身份、实现版本、SDK 版本和 Capability ID。
- `validate_manifest_capabilities()`：校验 Manifest 声明与 Provider Descriptor 完全一致。

## Agent、Tool 与 Plugin

Agent 和 Tool 共享 Descriptor、Context、Result 和 Error，但保留不同执行语义：

```text
AgentSPI.invoke()  → 自主任务处理
ToolSPI.execute() → 明确、单步、确定性操作
```

Plugin 是发现、打包和生命周期单位，不是执行语义。一个 Plugin 可以只提供 Agent、只提供 Tool，也可以提供多个不同类型的 Provider；每个 Provider 仍有独立 Capability ID。

这里刻意不存在万能的 `Plugin.execute()`。

## 生命周期契约

- `manifest()`、`capabilities()` 在插件存活期间应稳定且无副作用。
- `initialize()`、`shutdown()` 由 Loader 调用，插件实现必须幂等。
- Plugin Manifest 中的 Capability ID 不允许为空或重复。
- Agent/Tool 最终都必须返回 `ResultEnvelope`。

## 依赖边界

业务插件只能面向本模块和 `harness-contracts` 编程，不依赖 Runtime、Registry、Policy、Trace 或 Bootstrap。

## 测试

项目安装后运行：

```bash
.venv/bin/python -m unittest discover -s harness-spi/tests -v
```

## 阶段一非目标

不实现远程 Agent、MCP、HTTP Provider、流式调用或热升级。
