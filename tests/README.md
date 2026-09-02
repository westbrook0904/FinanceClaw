# Tests

当前回归基线覆盖 Stage-0 框架兼容切片、Stage-1 Execution Spine，以及仍由 FinanceClaw
保留的 Context、Memory、Policy、Trace、Events 契约。

```bash
.conda/envs/stage0/bin/python -m pytest -q
```

`tests/stage1` 重点验证：

- ToolGovernance、不可变 Catalog、Policy 与 TargetResolver；
- Direct READ retry/failure 和 WRITE interrupt/approve/reject/edit/reapproval；
- Agent Tool Calling、模型可见性过滤、执行时二次鉴权、model fallback；
- MCP 治理覆盖；
- BFF 认证上下文、idempotency、run/status/resume/SSE；
- 新代码与已删除旧 Runtime/Registry/SPI 的依赖隔离。

真实 Provider、LangSmith 与 Agent Server 网络联调由显式 smoke/probe 命令执行，不用 mock 结果
冒充在线证据。
