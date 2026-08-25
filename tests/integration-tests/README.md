# integration-tests

阶段一集成覆盖主要位于：

- `harness-runtime/tests`：Request → Context → Registry → Policy → Capability → Result → Trace。
- `harness-bootstrap/tests`：Composition Root、Plugin Loader、Registry 和 Runtime 的组装生命周期。
- `plugins/tests`：三个真实示例插件通过 Bootstrap/Runtime 的端到端调用。

关键验收链路为：

```text
Plugin discovery
  ↓
initialize / register
  ↓
Request → Context → Registry → Policy → Agent/Tool
  ↓
ResultEnvelope + Trace
  ↓
unregister / shutdown
```

测试同时覆盖策略拒绝、Registry miss、Provider 异常、Tool 输入错误、超时、task 取消、Trace 关闭和启动批次回滚。
