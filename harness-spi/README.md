# harness-spi

## 职责

定义插件扩展点：统一的 Capability 描述，以及语义独立的 Agent、Tool、Plugin 接口和生命周期。

## 依赖边界

- 只允许依赖 `harness-contracts`。
- Agent 与 Tool 可共享描述、结果和错误，但不强行共享完全相同的执行语义。
- 不提供万能的 `Plugin.execute()` 接口。

## 阶段一非目标

不实现远程 Agent、MCP、HTTP Provider、流式调用或热升级。

## 公共接口

- `Capability`：只统一 `descriptor()`，不统一 Agent 与 Tool 的执行方法。
- `AgentSPI`：异步 `invoke(AgentRequest, InvocationContext)`。
- `ToolSPI`：异步 `execute(ToolRequest, InvocationContext)`。
- `PluginSPI`：提供 `manifest()`、`capabilities()`、`initialize()` 和 `shutdown()`；不提供万能执行入口。
- `PluginManifest`：声明插件身份、SDK 版本和 Capability ID；Loader 注册前应使用 `validate_manifest_capabilities()` 校验清单与 Provider 一致。

`manifest()`、`capabilities()` 应稳定且无副作用；生命周期方法应由 Loader 顺序调用，插件实现应保证初始化和关闭幂等。

## 运行测试

```bash
PYTHONPATH=harness-contracts/src:harness-spi/src \
  .venv/bin/python -m unittest discover -s harness-spi/tests -v
```
