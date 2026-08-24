# plugins

业务 Agent 与 Tool 的容器目录。每个插件独立声明能力，只允许依赖 `harness-contracts` 和 `harness-spi`，不得导入 Runtime、Registry 或 Policy 的内部实现。

阶段一预留 Echo Agent、Calculator Tool 和 Mock Finance Agent 三个示例位置；前两个用于验证通用能力，Mock Finance Agent 仅用于后续插件测试，不进入 Harness Core。
