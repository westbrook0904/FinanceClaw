# 旧 BFF 运行手册已替代

Stage 10 已删除独立 BFF、Webhook 和 `/v1/runs/*`。当前启动、授权、取消、SSE 与故障处理统一见 [Turn 运行手册](turn-control.md)，不再运行 `uvicorn main:app`。
