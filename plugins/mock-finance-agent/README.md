# mock-finance-agent

阶段一业务边界验证插件，实现 `finance.mock-query/v1`。

## 公共类型

- `MockFinanceAgent`：实现 `AgentSPI`。
- `MockFinanceAgentPlugin`：实现 `PluginSPI`，暴露一个稳定 MockFinanceAgent Provider。

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

它不访问真实行情、数据库、LLM 或其他数据源，也不执行真实金融分析。该插件用于证明加入财经领域 Capability 时，Harness Core 仍不需要财经类型或业务依赖。
