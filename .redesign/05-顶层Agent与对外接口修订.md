# 产品 API 与顶层 Agent

唯一启动路径为创建 Conversation，再提交 message-only Turn。根 Agent 根据消息回答或调用受治理 Tool。斜杠指令只是调用偏好。

- POST /v1/conversations
- POST /v1/conversations/{id}/turns（Idempotency-Key）
- GET /v1/conversations/{id} 与 /messages
- GET /v1/runs/{id} 与 /events（Last-Event-ID）
- POST /v1/runs/{id}/cancel
- POST /v1/runs/{id}/authorization 与 DELETE 同路径
- GET /v1/interactions/{id}
- POST /v1/interactions/{id}/responses（Idempotency-Key）
- GET /v1/runs/{id}/notifications 与 DELETE 同路径

查询和 SSE 只读取持久事实；后台循环提交、恢复与核对。子图正常完成直接返回顶层 Tool，人工决定才恢复根原生运行。交互响应统一使用 revision、类型、回答或决定及动作摘要。

内部回调路径为 /internal/webhooks/langgraph/{backend_instance_id}，使用固定服务认证。运维细节见 [BFF 手册](../docs/operations/bff-run-control.md)。
