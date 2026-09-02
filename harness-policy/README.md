# harness-policy

该包暂时只保留 Stage 3 所需的 `PRE_MEMORY_READ/WRITE/DELETE` 边界。

Tool 执行策略已迁移到 `financeclaw.tools.ToolPolicy`，由 Agent middleware 和
`DirectToolGraph` 共同执行。旧 `PRE_EXECUTE` Capability/Provider 策略已删除。
