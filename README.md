# FinanceClaw

FinanceClaw 基于 LangChain、LangGraph AgentServer 与 LangSmith，提供金融场景的会话、工具治理、人工交互、上下文、记忆、制品与审计。

业务 API 位于 AgentServer 的自定义 FastAPI 应用内，通过 `get_client(url=None, api_key=None)` 使用进程内 ASGI transport；独立的原生 queue worker 执行图。Stage 11 增加 memory_worker，在回答之外提取、整合长期记忆；integrations 负责渠道、通知和索引。四个角色使用同一镜像，公开根图只有 `finance_agent`。

一个 `turn_id` 对应一个业务任务，开始和每次人工恢复分别记录不可变 `turn_commands`。真实 `native_run_id` 只作为内部执行回执。应用库共 18 张表；运行控制保留 Turn、Command、Interaction，记忆新增 owner、source、extraction、record 四张表。SQL 是记忆事实源，原生 Store 是可重建索引；记忆确认不恢复业务 interrupt。

[包结构](docs/architecture/package-layout.md) · [Stage 11 设计](.redesign/stages/stage-11-异步记忆与上下文治理实施方案.md) · [Stage 11 实现与验证](.redesign/stages/stage-11-实现与验证.md) · [运行手册](docs/operations/turn-control.md)

## 启动

项目尚未上线，初始迁移只面向空应用库。`compose.yml` 使用独立的 `financeclaw-stage11` 项目和新卷，不会清空原有本地数据库。业务库与原生库分开初始化；Alembic 不管理 LangGraph 原生表。

```bash
cp config/environments/unified.env.example .env
cp config/environments/memory.env.example .env.memory
# 填写数据库密码、产品令牌、集成令牌和官方 AgentServer 所需凭据。
uv run --frozen python scripts/deploy.py
```

部署命令会为已启用 MCP 自动补齐缺失的工具定义，复用并检查已有定义，然后构建镜像、启动服务并显示容器状态。
需要更新远端工具定义时加 `--refresh-mcp`；只准备文件供审阅时加 `--prepare-only`。
普通容器重启仍使用镜像中的固定定义。命令选项见 [MCP 接入手册](docs/operations/mcp.md#发布到-docker)。

API 默认监听 `127.0.0.1:8000`。原生 API 必须配置 `N_JOBS_PER_WORKER=0`；Worker 必须配置正数并发并通过官方 `/storage/queue_entrypoint.sh` 启动。镜像入口会验证这些条件。

`.env.memory` 只包含记忆 Worker 的数据库密码、模型凭据和处理策略；其 `MEMORY_POSTGRES_PASSWORD` 必须与迁移进程一致。空库迁移后显式创建有限权限的记忆角色，不向记忆 Worker 传递应用管理员 DSN、产品令牌或原生 Store 凭据。

本地示例使用确定性离线模型和共享本地制品卷。真实 Provider、OIDC、加密 S3、飞书与观测配置见 [环境说明](config/environments/README.md) 和[本地完整链路](docs/operations/local-full-stack.md)。生产部署从 `production.env.example` 注入策略与密钥，使用不可变应用镜像摘要。

模型供应商、默认别名及 Agent / 摘要 / 记忆任务覆盖统一在 [config/models.toml](config/models.toml) 声明；未覆盖的用途使用默认模型。配置方式见[模型配置](docs/operations/model-configuration.md)。

通用 MCP 服务和 Agent 工具绑定在 [config/mcp.toml](config/mcp.toml) 声明。已提供 RollingGo 酒店/机票查询配置，模板默认关闭；配置凭据并启用后，部署命令会自动准备所需定义。授权与接入步骤见 [MCP 接入手册](docs/operations/mcp.md)。

内置 `market-brief` 行情简报和 `cocktail-from-what-i-have` 现有材料调酒技能。飞书单聊发送 `/skills` 打开表单，选择技能、填写任务描述后点击“开始执行”；选择仅对本次任务生效。飞书和 API 也可使用 `/skill 技能ID 任务正文` 直接提交，或由模型按需加载；权限和工具审批继续沿用当前任务。飞书与 curl 示例见 [Skills 运行手册](docs/operations/skills.md)，验证范围见 [实现与验证](.redesign/stages/skills-实现与验证.md)。

## 产品接口

先 `POST /v1/conversations`，再向 `POST /v1/conversations/{id}/turns` 提交 `{"message":"..."}` 和 `Idempotency-Key`，返回 202 与 `turn_id`。API 不接受用户指定的 thread、checkpoint、native run 或 callback。

- `GET /v1/conversations/{id}/turns/{turn_id}`：当前状态快照。
- `GET /v1/conversations/{id}/turns/{turn_id}/events`：`turn.snapshot` 与心跳；重连只返回最新 revision。
- `POST .../cancel`：记录取消意图，确认原生停止后才进入 cancelled。
- `POST/DELETE .../authorization`：带 `expected_grant_revision` 和幂等键的有限授权更新。
- `GET /v1/interactions/{id}`、`POST /v1/interactions/{id}/responses`：读取问题与提交 typed response。
- `GET /v1/conversations/{id}/messages`：按 `after`、`limit` 分页读取永久 Journal。
- `/v1/memory/settings`、`/v1/memories`：记忆开关、分页查询、创建、纠正和遗忘。
- `POST /v1/memory/candidates/{id}/decision`：独立确认或拒绝冻结版本的候选。
- `/v1/health/live`、`/v1/health/ready`：业务进程健康。

最终答案、Turn 状态、审计、通知、历史索引和记忆提取意图在一个事务中提交。观察任务独立于客户端，浏览器断开不会停止执行。未知提交会保留为 uncertain 并查找原回执，不自动重新发送。失败或确认取消后的下一轮使用干净 thread，已完成历史仍由 Journal 保留。

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

Stage 11 将有限画像按 Turn 冻结，历史任务按需召回；已完成的轮内工具片段可在归档后压缩为 checkpoint WorkingContext，真实用户输入和未完成工具配对继续受保护。详见[上下文与记忆运维](docs/operations/context-budget.md)。领域能力包括[紫微候选](docs/operations/ziwei-agent.md)；`/tool`、`/agent`、`/workflow` 是消息中的调用偏好，子图始终使用同一 Turn 的预算和授权。
