# calculator-tool

阶段一确定性 Tool 插件，实现 `math.calculate/v1`。

## 公共类型

- `CalculatorTool`：实现 `ToolSPI`。
- `CalculatorToolPlugin`：实现 `PluginSPI`，暴露一个稳定 CalculatorTool Provider。

## 输入

Runtime 要求 Tool 的 `RequestInput.content` 为 JSON object，并将其转换为 `ToolRequest.arguments`：

```json
{
  "operation": "add",
  "left": 1,
  "right": 2
}
```

支持 `add`、`subtract`、`multiply`、`divide`。`left` 和 `right` 必须是 `int` 或 `float`，布尔值不会被当作数字接受。

## 输出与错误

成功时返回 `ResultOutput(type="number", data=<结果>)`，metadata 包含 Capability ID、操作名和 Request ID。

| 场景 | 错误码 |
|---|---|
| 参数不是数字 | `PLUGIN.CALCULATOR.INVALID_ARGUMENT` |
| 不支持的操作 | `PLUGIN.CALCULATOR.INVALID_OPERATION` |
| 除零 | `PLUGIN.CALCULATOR.DIVISION_BY_ZERO` |

插件抛出的 `CapabilityError` 由 Runtime 统一转换为失败 `ResultEnvelope` 并记录到 Trace。
