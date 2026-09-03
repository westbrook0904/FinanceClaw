# harness-contracts

Stage-3 已迁走旧 Runtime 与 Memory 契约。本包只临时保留仍被 `harness-events` 和
`harness-trace` 使用的最小事件/追踪类型，并将在 Stage 5 清理。

Capability、Provider、Selection、Retry、Approval、ResultEnvelope 与旧 RequestTarget 已删除；
框架内部对象不再复制为通用 Harness contract。
