# plugins

阶段一业务插件目录。Harness Core 不导入这里的任何实现；插件只依赖
`harness-contracts` 与 `harness-spi`，通过 `PluginSPI` 暴露能力。

当前示例插件：

- `echo-agent`：`echo.reply/v1`，原样回显输入，验证 Agent 链路。
- `calculator-tool`：`math.calculate/v1`，执行确定性四则运算，验证 Tool 链路。
- `mock-finance-agent`：`finance.mock-query/v1`，返回模拟财经结果，验证业务边界隔离。

安装项目后，这三个插件通过 `financeclaw.plugins` Python entry point 被
`LocalPluginProvider` 自动发现。也可以在测试或嵌入场景中把插件实例显式传给
`build_harness(plugins=...)`。

## 依赖红线

插件允许依赖：

```text
harness-contracts
harness-spi
```

插件禁止依赖 Runtime、Registry、Policy、Trace、Bootstrap 的具体实现。需要新增
Agent 或 Tool 时，应复制这个边界，而不是把业务逻辑写回 Harness Core。
