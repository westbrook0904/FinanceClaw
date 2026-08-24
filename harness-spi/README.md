# harness-spi

## 职责

定义插件扩展点：统一的 Capability 描述，以及语义独立的 Agent、Tool、Plugin 接口和生命周期。

## 依赖边界

- 只允许依赖 `harness-contracts`。
- Agent 与 Tool 可共享描述、结果和错误，但不强行共享完全相同的执行语义。
- 不提供万能的 `Plugin.execute()` 接口。

## 阶段一非目标

不实现远程 Agent、MCP、HTTP Provider、流式调用或热升级。
