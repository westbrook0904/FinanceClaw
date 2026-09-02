# integration-tests

Stage-1 集成主链：

```text
Client → FinanceClaw BFF → Agent Server thread/run/stream
                              ├─ finance_agent → governed BaseTool
                              └─ direct_tool → validate → authorize
                                               → interrupt/resume → execute → project
```

确定性集成测试位于 `tests/stage1`。真实 Agent Server 联调执行：

```bash
FINANCECLAW_OFFLINE_MODEL=true FINANCECLAW_DEBUG_FULL_IO=false \
  .conda/envs/stage0/bin/langgraph dev --no-browser --no-reload --port 2024

.conda/envs/stage0/bin/python -m financeclaw.application.server_smoke
```

该 smoke 覆盖默认 Agent stream、Direct READ、WRITE interrupt、edit 后重新 interrupt 和 approve。
