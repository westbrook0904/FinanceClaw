# mock-finance-agent

业务边界验证 Agent 插件，实现 `finance.mock-query/v1`。它是 Stage 2
`finance-review-plan` 的真实内置 Capability 之一，用于证明财经业务仍完全位于
Harness Core 之外。

## 公共类型

- `MockFinanceAgent`：实现 `AgentSPI`。
- `MockFinanceAgentPlugin`：实现 `PluginSPI`，暴露一个稳定 Provider。

## 行为

Agent 返回确定性的 JSON mock 结果：

```json
{
  "mock": true,
  "message": "mock finance agent executed",
  "input": {
    "type": "text",
    "content": "..."
  }
}
```

Result metadata 包含 Capability ID、Request ID 和 `data_source=none`。Descriptor 使用
无副作用执行画像默认值。

该插件不访问真实行情、数据库、LLM 或其他数据源，也不执行真实金融分析。它可通过 Direct
Invocation 调用，也可作为 Plan Node 参与并行、Join、Checkpoint/Resume 和输出 Binding；
这些执行语义全部由 Harness 提供。
