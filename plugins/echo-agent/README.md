# echo-agent

阶段一最小 Agent 插件，实现 `echo.reply/v1`。

行为：

- 输入：任意 `RequestInput`。
- 输出：保持原 `type` 和 `content` 原样回显。
- 不访问网络、不持久化状态、不调用其他 Capability。
- `initialize()` / `shutdown()` 幂等。

它的目的不是提供业务能力，而是验证 `Plugin → Registry → Runtime → AgentSPI → ResultEnvelope`
整条链路。
