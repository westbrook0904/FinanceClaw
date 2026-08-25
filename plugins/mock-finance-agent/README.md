# mock-finance-agent

用于验证业务插件边界的模拟财经 Agent，实现 `finance.mock-query/v1`。

该插件只返回确定性的 mock 结果，不访问真实行情、数据库、LLM 或其他数据源，也不做真实
金融分析。它存在的主要目的，是证明即使加入财经领域 Capability，Harness Core 也不需要
引入任何财经类型或业务依赖。
