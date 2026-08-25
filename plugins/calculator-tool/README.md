# calculator-tool

阶段一确定性 Tool 插件，实现 `math.calculate/v1`。

输入 `RequestInput.content` 必须是 JSON object：

```json
{
  "operation": "add",
  "left": 1,
  "right": 2
}
```

支持 `add`、`subtract`、`multiply`、`divide`。无效参数和除零使用
`PLUGIN.CALCULATOR.*` 错误码，由 Runtime 统一归一化为 `ResultEnvelope`。
