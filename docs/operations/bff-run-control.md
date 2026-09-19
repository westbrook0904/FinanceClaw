# 旧 BFF 手册入口

当前产品已使用统一 AgentServer API。独立 BFF、Webhook 进程和 `/v1/runs/*` 属于旧架构，不再使用 `uvicorn main:app` 启动。

- 第一次配置、启动和发请求：[本地完整链路](local-full-stack.md)。
- 当前四个角色、Turn 状态、取消、SSE 与恢复：[Turn 运行手册](turn-control.md)。
- 发布、健康检查和回退：[生产运行手册](production-runbook.md)。

此文件保留旧链接的入口作用，不描述一个可继续部署的 BFF 版本。
