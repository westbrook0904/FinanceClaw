# echo-agent

确定性最小 Agent 插件，实现 `echo.reply/v1`。它既用于 Direct Invocation 回归，也作为
Stage 2 Plan 的普通 Capability Node，验证输出 Binding、Approval 后续节点和 Trace
传播；Stage 3A Acceptance 同时用它守住旧 Plugin 的多 Provider Registry 兼容性。

## 公共类型

- `EchoAgent`：实现 `AgentSPI`。
- `EchoAgentPlugin`：实现 `PluginSPI`，暴露一个稳定 EchoAgent Provider。

## 行为

- 输入：任意 `RequestInput`。
- 输出：保持输入的 `type` 和 `content` 原样回显。
- Result metadata 包含 Capability ID 和 Request ID。
- Descriptor 标记为本地、确定性示例 Agent，执行画像使用无副作用默认值。
- 不访问网络、不持久化状态、不调用其他 Capability。
- `initialize()/shutdown()` 幂等。

该插件验证：

```text
Plugin discovery → Registry → CapabilityInvoker → AgentSPI.invoke
                 → ResultEnvelope → Plan Binding / Trace
```

DAG 调度、Retry、Policy 和 Checkpoint 由 Harness 负责，插件不感知当前调用来自 Direct
Invocation 还是 ExecutionPlan。
