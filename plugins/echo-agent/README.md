# echo-agent

阶段一最小 Agent 插件，实现 `echo.reply/v1`，用于验证完整 Agent 调用链。

## 公共类型

- `EchoAgent`：实现 `AgentSPI`。
- `EchoAgentPlugin`：实现 `PluginSPI`，暴露一个稳定 EchoAgent Provider。

## 行为

- 输入：任意 `RequestInput`。
- 输出：保持输入的 `type` 和 `content` 原样回显。
- Result metadata 包含 Capability ID 和 Request ID。
- Descriptor 标记为本地、确定性示例 Agent。
- 不访问网络、不持久化状态、不调用其他 Capability。
- `initialize()` / `shutdown()` 幂等。

该插件验证：

```text
Plugin discovery → Registry → Runtime → AgentSPI.invoke → ResultEnvelope → Trace
```
