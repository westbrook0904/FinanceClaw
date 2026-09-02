# integration-tests

Stage-2 集成主链：

```text
Client → FinanceClaw BFF → Agent Server thread/run/stream
                              ├─ finance_agent → governed BaseTool
                              └─ direct_tool → validate → authorize
                                               → interrupt/resume → execute → project
       ↘ Conversation Journal / Summary / Manifest / Artifact metadata
```

确定性集成测试位于 `tests/stage1` 与 `tests/stage2`。真实 Agent Server 联调执行：

```bash
FINANCECLAW_OFFLINE_MODEL=true FINANCECLAW_DEBUG_FULL_IO=false \
  .conda/envs/stage0/bin/langgraph dev --no-browser --no-reload --port 2024

.conda/envs/stage0/bin/python -m financeclaw.application.server_smoke
```

该 smoke 覆盖默认 Agent stream、Direct READ、WRITE interrupt、edit 后重新 interrupt 和 approve。

Conversation/Thread 跨重启 smoke：

```bash
.conda/envs/stage0/bin/python -m financeclaw.application.conversation_smoke \
  --idempotency-key before-restart

# 重启 Agent Server 后复用第一次输出的 conversation_id
.conda/envs/stage0/bin/python -m financeclaw.application.conversation_smoke \
  --conversation-id <conversation_id> --idempotency-key after-restart
```

第二次输出必须保持相同 `conversation_id`/`thread_id`，并把 `message_count` 和
`manifest_count` 分别从 2 增长到 4。
