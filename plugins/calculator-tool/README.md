# calculator-tool

确定性 Tool 插件，实现 `math.calculate/v1`。它既支持 Direct Invocation，也作为 Stage 2
Plan Node 验证结构化输入 Binding、并行执行、Join 和错误传播；Stage 3A 下无需修改即可
由 Loader 生成稳定 Provider ID。

## 公共类型

- `CalculatorTool`：实现 `ToolSPI`。
- `CalculatorToolPlugin`：实现 `PluginSPI`，暴露一个稳定 CalculatorTool Provider。

## 输入

CapabilityInvoker 要求 Tool 的 `RequestInput.content` 为 JSON object，并将其转换为
`ToolRequest.arguments`：

```json
{
  "operation": "add",
  "left": 1,
  "right": 2
}
```

支持 `add`、`subtract`、`multiply`、`divide`。`left/right` 必须是
`int` 或 `float`，布尔值不会被当作数字。

## 输出与错误

成功返回 `ResultOutput(type="number", data=<结果>)`；metadata 包含 Capability ID、
操作名和 Request ID。

| 场景 | 错误码 |
|---|---|
| 参数不是数字 | `PLUGIN.CALCULATOR.INVALID_ARGUMENT` |
| 不支持的操作 | `PLUGIN.CALCULATOR.INVALID_OPERATION` |
| 除零 | `PLUGIN.CALCULATOR.DIVISION_BY_ZERO` |

插件抛出的 `CapabilityError` 由 CapabilityInvoker 统一转换为失败
`ResultEnvelope` 并记录到 Trace。节点 FailurePolicy、Retry 和 Plan PARTIAL 语义由
ExecutionEngine 决定。

Descriptor 使用无副作用执行画像默认值，不访问网络或持久化状态。
