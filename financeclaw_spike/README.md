# Stage-0 Framework Spike

本包只验证框架兼容性，不承载真实金融业务，也不引用旧 Harness Runtime。

## 组成

- `graph.py`：`create_agent`、model retry/fallback、READ retry、WRITE HITL、动态工具过滤；
- `tools.py`：最小 READ/WRITE LangChain `BaseTool`；
- `context.py`：对模型隐藏的 trusted runtime context；
- `observability.py`：完整开发 I/O、安全脱敏和 LangSmith 自定义 child run；
- `mcp_server.py` / `mcp.py`：stateless MCP 演示与本地 governance overlay；
- `infrastructure.py`：PostgreSQL、Redis、checkpointer 与 Store 探针；
- `provider.py`：真实模型 Tool Calling 与 structured output 探针；
- `server_smoke.py`：Agent Server thread/run/SSE/checkpoint/HITL smoke。

## 模式

默认使用 `.env` 中配置的真实 Provider。只有本地确定性测试与 Agent Server smoke 可以设置：

```text
FINANCECLAW_SPIKE_OFFLINE_MODEL=true
```

production 配置若开启完整 I/O 或 offline model 会 fail fast。日志和 masking helper 会删除
API Key、Authorization、Cookie、Token、数据库 DSN 等敏感值。

## 外部门禁

需要真实 Secret 或服务的 pytest 用 `external` marker 标记，并在对应环境变量不存在时跳过。
它们不是 mock 成功：提交前应在有凭证/基础设施的环境补跑并把结果写入 Stage-0 验证记录。
