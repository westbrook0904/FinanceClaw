# FinanceClaw

FinanceClaw 基于 LangChain、LangGraph AgentServer 与 LangSmith，提供金融场景的会话、工具治理、人工交互、上下文、记忆、制品与审计。

Stage 10 将业务 API 合入 AgentServer 的自定义 FastAPI 应用。API 通过 `get_client(url=None, api_key=None)` 使用进程内 ASGI transport；独立的原生 queue worker 执行图。飞书连接、通知投递和历史索引由 integrations 进程负责。三个角色使用同一镜像，公开根图只有 `finance_agent`。

一个 `turn_id` 对应一个业务任务，开始和每次人工恢复分别记录不可变 `turn_commands`。真实 `native_run_id` 只作为内部执行回执。应用库共 14 张表，运行控制仅保留 Turn、Command、Interaction 三种事实；没有独立 BFF 服务、Webhook、运行进度历史表或旧接口兼容层。

[包结构](docs/architecture/package-layout.md) · [Stage 10 设计](.redesign/stages/stage-10-统一API与运行模型收敛实施方案.md) · [实现与验证](.redesign/stages/stage-10-实现与验证.md) · [运行手册](docs/operations/turn-control.md)

## 启动

项目尚未上线，初始迁移只面向空应用库。`compose.yml` 使用独立的 `financeclaw-stage10` 项目和新卷，不会清空或复用原有本地数据库。业务库与原生库分开初始化；Alembic 不管理 LangGraph 原生表。

```bash
cp config/environments/unified.env.example .env
# 填写数据库密码、产品令牌、集成令牌和官方 AgentServer 所需凭据。
docker compose build
docker compose up -d
```

API 默认监听 `127.0.0.1:8000`。原生 API 必须配置 `N_JOBS_PER_WORKER=0`；Worker 必须配置正数并发并通过官方 `/storage/queue_entrypoint.sh` 启动。镜像入口会验证这些条件。

本地示例使用确定性离线模型和共享本地制品卷。真实 Provider、OIDC、加密 S3、飞书与观测配置见 [环境说明](config/environments/README.md) 和[本地完整链路](docs/operations/local-full-stack.md)。生产部署从 `production.env.example` 注入策略与密钥，使用不可变应用镜像摘要。

## 产品接口

先 `POST /v1/conversations`，再向 `POST /v1/conversations/{id}/turns` 提交 `{"message":"..."}` 和 `Idempotency-Key`，返回 202 与 `turn_id`。API 不接受用户指定的 thread、checkpoint、native run 或 callback。

- `GET /v1/conversations/{id}/turns/{turn_id}`：当前状态快照。
- `GET /v1/conversations/{id}/turns/{turn_id}/events`：`turn.snapshot` 与心跳；重连只返回最新 revision。
- `POST .../cancel`：记录取消意图，确认原生停止后才进入 cancelled。
- `POST/DELETE .../authorization`：带 `expected_grant_revision` 和幂等键的有限授权更新。
- `GET /v1/interactions/{id}`、`POST /v1/interactions/{id}/responses`：读取问题与提交 typed response。
- `GET /v1/conversations/{id}/messages`：按 `after`、`limit` 分页读取永久 Journal。
- `/v1/health/live`、`/v1/health/ready`：业务进程健康。

最终答案、Turn 状态、审计、通知和历史索引意图在一个事务中提交。观察任务独立于客户端，浏览器断开不会停止执行。未知提交会保留为 uncertain 并查找原回执，不自动重新发送。失败或确认取消后的下一轮使用干净 thread，已完成历史仍由 Journal 与 Stage 9 机制保留。

外部普通用户不能直接操作原生 thread/run/store。integrations 凭据只开放标准化渠道入口与限定 namespace 的 Store 维护；checkpoint 回收另需用户的 `maintenance:checkpoints` 权限并验证归档会话已无待办。

## 开发与验证

```bash
uv sync --frozen --extra dev --extra ziwei
.venv/bin/pytest -q
.venv/bin/ruff check financeclaw tests scripts deploy experiments/stage10
.venv/bin/ruff format --check financeclaw tests scripts deploy experiments/stage10
.venv/bin/python scripts/check_secret_leaks.py
```

持久化 API/Worker、故障恢复、权限和 PostgreSQL 并发探针见 [experiments/stage10](experiments/stage10/README.md)。探针使用隔离数据库和合成输入，不发送真实飞书消息。

Stage 9 的当前 Turn 保护、原生工作摘要、画像直读、每 Turn 召回、工具结果归档和历史按需读取继续成立，详见[上下文与记忆运维](docs/operations/context-budget.md)。领域能力包括[紫微候选](docs/operations/ziwei-agent.md)；`/tool`、`/agent`、`/workflow` 是消息中的调用偏好，子图始终使用同一 Turn 的预算和授权。
