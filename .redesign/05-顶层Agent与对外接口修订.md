# Stage 10 产品 API

唯一启动路径是创建 Conversation，再提交带 Idempotency-Key 的 message-only Turn。公开根图为 `finance_agent`；领域 Agent 和 Workflow 都是内部 Tool/subgraph，不创建独立业务任务。

当前接口与示例见 [README](../README.md#产品接口)，完整语义见 [Turn 运行手册](../docs/operations/turn-control.md)。所有 Turn 查询、取消和授权路径都嵌套在 Conversation 下，必须同时验证两者的归属关系。

HTTP/SSE 查询只读；人工交互使用 interaction_id、revision 和 typed response，不能提供原生 thread/run/checkpoint。回复决定必须在返回 202 前持久化。

内部飞书路径 `/internal/channels/feishu/events` 仅接受受信任集成身份的标准化消息或卡片回调。不存在旧 Webhook、`/v1/runs/*` 或业务 `run_id` 别名。checkpoint 回收作为独立产品维护能力，只允许具有专门 scope 的用户操作自己已归档且无待办的会话。
