# 本地启动与第一次请求

本文从仓库根目录启动当前的统一镜像部署。先让 HTTP 会话链路正常工作，再按需要启用飞书、真实模型和领域工具。完整配置说明见[环境配置](../../config/environments/README.md)，运行状态与恢复语义见 [Turn 运行手册](turn-control.md)。

## 1. 准备环境与选择功能

需要 Python 3.13、uv、Docker Engine / Docker Desktop 与 Compose v2。官方持久 AgentServer 镜像还需要有效的自托管凭据；离线模型开关不会免除运行时的凭据要求。

```bash
uv sync --frozen --extra dev --extra ziwei
# 仅在目标文件不存在时复制，避免覆盖已有本地配置。
test -e .env || cp config/environments/unified.env.example .env
test -e .env.memory || cp config/environments/memory.env.example .env.memory
```

编辑两个本地环境文件，至少完成以下配置。示例值是占位符，不能直接作为真实凭据使用；不要把环境文件或展开后的 Compose 配置提交到仓库。

| 配置 | 放在哪里 | 用途 |
|---|---|---|
| `POSTGRES_PASSWORD` | `.env` | 本地 PostgreSQL 应用与原生数据库身份 |
| `MEMORY_POSTGRES_PASSWORD` | `.env` 与 `.env.memory`，值一致 | 独立 `financeclaw_memory` 身份，至少 24 个字符 |
| `LANGSMITH_API_KEY` 或运行时要求的 license 配置 | `.env` | 官方持久 AgentServer 启动凭据 |
| `FINANCECLAW_API_AUTH_TOKEN` | `.env` | 本地 HTTP 产品访问令牌 |
| `FINANCECLAW_INTEGRATION_SERVICE_TOKEN` | `.env` | integrations 内部调用，至少 32 个字符 |
| `FINANCECLAW_OFFLINE_MODEL` | 两个文件 | 初次验证可保持 `true`；真实模型的配置和密钥见[模型手册](model-configuration.md) |

**当前仓库的 `config/mcp.toml` 已启用 RollingGo 酒店服务。** 启动前二选一：在 `.env` 配置 `FINANCECLAW_ROLLINGGO_API_KEY`，或将 `[servers.rollinggo_hotel]` 的 `enabled` 显式改为 `false`，先运行本地基础链路。禁用服务不需要删除其 Agent 绑定。启用酒店工具后，调用者还需要 `travel:read` scope；模型离线模式不会自动禁用外部 MCP。

统一环境示例中飞书、紫微和 Taibu 默认关闭，Skills 默认开启。基础验证无需开启这些外部或领域能力。

## 2. 构建与启动

```bash
docker compose config --quiet
uv run --frozen python scripts/deploy.py
docker compose ps -a
```

部署脚本依次准备已启用的 MCP 固定定义、构建镜像、启动 Compose；任一步失败都会停止后续步骤。`--prepare-only` 只准备 MCP，仍可能访问启用的 MCP 服务；`--refresh-mcp` 显式刷新已有定义。其他环境文件、Compose 叠加和构建代理参数见 [MCP 部署说明](mcp.md#发布到-docker)。

默认 Compose 项目名为 `financeclaw-stage11`。首次运行创建应用库 `financeclaw_app`、原生库 `financeclaw_native`，以及 PostgreSQL、Redis、Artifact 卷。原生库由 AgentServer 管理；`migrate` 执行业务迁移并配置记忆工作进程的数据库权限。

| 服务 | 正常结果 | 职责 |
|---|---|---|
| `postgres`、`redis` | `healthy` | 业务/原生持久化与原生队列 |
| `migrate` | `Exited (0)` | 一次性迁移与记忆数据库身份配置 |
| `api` | `healthy` | `/v1/*` 产品接口、受理、状态核对；不执行图任务 |
| `worker` | `healthy` | 原生持久图执行，默认 4 个执行槽 |
| `integrations` | `healthy` | 历史/记忆索引；启用飞书后负责连接与通知 |
| `memory_worker` | `healthy` | 独立的记忆提取与整理 |

`up -d` 返回只表示服务启动命令已提交。若 `api` 尚未就绪，依赖它的 Worker / integrations 可能仍为 `Created`；先查 API 的最早错误。

## 3. 检查健康并创建请求

```bash
curl --noproxy '*' --fail http://127.0.0.1:8000/v1/health/live
curl --noproxy '*' --fail http://127.0.0.1:8000/v1/health/ready
```

预期分别包含 `status: "ok"` 和 `ready: true`。若修改了 `API_PORT`，同步替换 URL。就绪检查覆盖应用数据库、Artifact、原生 SDK 和 API 后台责任；还应确认其他三个角色自身的健康状态。

以下命令使用已经安全注入当前终端的 `FINANCECLAW_API_AUTH_TOKEN`。先保存创建响应中的 `conversation_id`，再将两个示例 ID 替换为响应中的实际值：

```bash
curl --noproxy '*' --fail-with-body -X POST http://127.0.0.1:8000/v1/conversations \
  -H "Authorization: Bearer ${FINANCECLAW_API_AUTH_TOKEN}" \
  -H 'Content-Type: application/json' -d '{}'

curl --noproxy '*' --fail-with-body -X POST \
  http://127.0.0.1:8000/v1/conversations/CONVERSATION_ID/turns \
  -H "Authorization: Bearer ${FINANCECLAW_API_AUTH_TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: first-local-turn-001' \
  -d '{"message":"你好，请介绍你能做什么。"}'

curl --noproxy '*' --fail-with-body \
  http://127.0.0.1:8000/v1/conversations/CONVERSATION_ID/turns/TURN_ID \
  -H "Authorization: Bearer ${FINANCECLAW_API_AUTH_TOKEN}"
```

创建会话返回 HTTP 201；创建 Turn 返回 HTTP 202 和 `turn_id`，此时任务可能仍在排队。轮询快照可看到 `completed`、`waiting` 或明确的失败状态；`waiting` 需要回答快照中的交互。同一消息重试复用原幂等键；新消息使用新键。离线模型只验证流程，不能用来评价自由对话、真实行情或推荐质量。

## 4. 接入飞书单聊

HTTP 请求完成后再接入飞书，便于把业务执行问题与渠道投递问题分开定位。

1. 在飞书开发者后台为机器人启用长连接（WebSocket），订阅 `im.message.receive_v1` 与 `card.action.trigger`，配置发送消息及 CardKit 所需权限。
2. 在 `.env` 设置 `FINANCECLAW_FEISHU_ENABLED=true`，填写 `FINANCECLAW_FEISHU_APP_ID`、`FINANCECLAW_FEISHU_APP_SECRET`；将允许用户写入 `FINANCECLAW_FEISHU_ALLOWED_OPEN_IDS` JSON 数组，并按需要配置 `FINANCECLAW_FEISHU_SCOPES`。生产使用 `FINANCECLAW_FEISHU_SECURITY_MODE=strict`。
3. 确认 API 与 integrations 的渠道配置一致，内部 API 地址和服务令牌可用。通过部署入口重新创建相关服务；默认仅一个 integrations 建立飞书连接。
4. 先检查 integrations 健康，再由允许列表中的用户发一条合成单聊消息；核对同一任务卡的受理、进度与最终回答。随后分别验证澄清/审批表单、停止按钮、长回答和 `/skills` 表单。

完整配置表、卡片机制、平台组件参考和故障定位见[飞书任务卡与通知](notifications.md)。HTTP 离线测试通过不代表真实飞书租户的权限和按钮回调已通过。

## 5. 常见问题

| 现象 | 先查哪里 | 处理方向 |
|---|---|---|
| MCP 准备失败、缺少认证 | `config/mcp.toml` 与启用服务的密钥变量名 | 补齐密钥或显式关闭不用的服务；不要把密钥写进 TOML |
| `migrate` 失败 | `docker compose logs migrate` | 区分连接失败、密码不足与旧 schema；不要删除数据库来掩盖原因 |
| `api` 不健康 | `docker compose logs --tail=200 api` | 找最早的数据库、Artifact、出站策略或运行时凭据错误 |
| Worker 一直不启动 | `docker compose ps -a`，随后查 API | Compose 在等待 API 健康，不一定是 Worker 崩溃 |
| HTTP 本机访问被代理拦截 | 使用上面的 `--noproxy '*'` | 本机地址不经过外部代理 |
| 容器访问宿主机服务失败 | 容器 URL 与主机 allowlist | 使用 `host.docker.internal`，并显式允许对应主机；容器的 `localhost` 指容器本身 |
| 任务完成但飞书没有更新 | [通知排查](notifications.md) | 分开核对 Turn 状态和卡片投递状态 |
| 记忆/检索没有更新 | [Outbox 排查](memory-outbox.md) | 检查消费者、任务状态、模型指纹与向量维度 |

```bash
docker compose logs --tail=200 migrate api worker integrations memory_worker
docker compose exec integrations python -m financeclaw.integrations.health
docker compose exec memory_worker python -m financeclaw.memory_worker.health
```

本地 PostgreSQL 暴露在 `127.0.0.1:5433`，用户名为 `financeclaw`，密码来自 `.env`，数据库按用途选择 `financeclaw_app` 或 `financeclaw_native`。容器之间使用 `postgres:5432`。日常业务查询不要修改原生运行时的内部表。

## 6. 更新与停止

配置、模型档案和工具契约会影响发布指纹。更新前先处理在途任务，API 与 Worker 使用同一镜像和发布配置；记忆 Worker 使用匹配的记忆模型档案。源码或打包配置变化后重新执行部署命令，单纯 `restart` 不会重建镜像或载入新的容器环境。

```bash
docker compose stop
```

停止会保留数据。日常重启不使用 `down -v`；当前初始迁移不是旧开发 schema 的通用升级器。重新做空库验证时应明确选择新的项目资源，并另外安排宿主端口，不能假定改项目名就能同时占用固定的 `5433` 端口。

默认 Artifact 位于共享卷 `/data/artifacts`。若改用 S3/MinIO，需要自己准备对象桶、访问凭据、加密配置和允许主机；根 Compose 不包含 MinIO 服务。生产部署继续阅读[生产运行手册](production-runbook.md)。
