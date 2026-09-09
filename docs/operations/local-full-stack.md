# 本地完整链路启动

这套配置用于在 macOS 宿主机运行 BFF，同时由 `langgraph up` 在 Docker 中运行
Agent Server。PostgreSQL 和 MinIO 也运行在 Docker 中，远程模型使用 DeepSeek，
Agent/模型调用追踪发送到 LangSmith。

## 运行拓扑

| 组件 | 运行位置 | 地址或存储 |
|---|---|---|
| BFF | 宿主机 | `http://127.0.0.1:8000` |
| Agent Server | Docker（由 LangGraph CLI 管理） | `http://127.0.0.1:2024` |
| BFF 业务库 | Docker PostgreSQL | `financeclaw_app`，宿主机端口 `5432` |
| Agent Server 状态库 | 同一 Docker PostgreSQL | `financeclaw_agent` |
| Agent Server 队列 | Docker（由 LangGraph CLI 管理） | CLI 内置 Redis |
| 共享制品存储 | Docker MinIO | API `9000`，Console `9001` |
| 模型 | 远程 DeepSeek | `https://api.deepseek.com` |
| Trace | 远程 LangSmith | project `financeclaw-local` |

`financeclaw_app` 和 `financeclaw_agent` 必须保持独立：前者保存会话、审计、工作流等
业务数据，后者由 Agent Server 管理 thread、run、checkpoint 和 store。

## 1. 准备依赖和私密配置

需要 Python 3.13、Docker Desktop，以及可用的 DeepSeek API key 和 LangSmith API key。
仓库已有 `.venv` 时可以直接使用；否则执行：

```bash
uv sync --all-extras --frozen --python 3.13
```

从模板生成两份不会被 Git 跟踪的配置：

```bash
cp config/environments/local-bff.env.example .env.bff.local
cp config/environments/local-agent-server.env.example .env.agent-server.local
```

在两份文件中同步替换：

- `replace-with-deepseek-api-key`
- `replace-with-langsmith-api-key`
- 如果 LangSmith key 可访问多个 workspace，再取消注释并填写 `LANGSMITH_WORKSPACE_ID`

本地 profile 会向 LangSmith 发送完整输入输出。若内容敏感，把两份文件中的
`FINANCECLAW_DEBUG_FULL_IO` 改为 `false`，并把两组 `*_HIDE_INPUTS`、
`*_HIDE_OUTPUTS` 改为 `true`。

## 2. 启动 PostgreSQL 和 MinIO

MinIO 使用本地静态 KMS 密钥支持客户端要求的 SSE-S3（`AES256`）加密。
首次配置时执行下面的命令生成 32 字节随机密钥：

```bash
openssl rand -base64 32
```

在仓库根目录新建 `.env.minio-kms.local`，保存以下一行，把占位符替换为命令输出：

```text
financeclaw-local:<生成的Base64密钥>
```

该文件是 MinIO 原始密钥文件，不是 dotenv 的 `KEY=VALUE` 格式。文件已被 Git 和
Docker 构建忽略，由 Compose secret 只读挂载到 MinIO。限制本机文件权限：

```bash
chmod 600 .env.minio-kms.local
```

已存在的密钥必须复用；重启、重新构建和恢复备份时不要重新生成。请与数据卷备份配套
保管密钥，丢失密钥将无法读取已加密对象。该静态密钥方案用于本地测试，正式部署应使用
独立的 KMS/KES 服务；它与紫微 HMAC 密钥分别管理。

确认 Docker Desktop 已启动，然后运行：

```bash
docker compose -f compose.local.yml up -d --wait postgres artifact-store
docker compose -f compose.local.yml run --rm artifact-init
docker compose -f compose.local.yml ps
```

首次创建 volume 时，初始化脚本会创建两个数据库并启用 pgvector，同时
`artifact-init` 会创建私有 bucket `financeclaw-artifacts`，并开启默认 SSE-S3 加密。
默认加密对后续写入生效，不会自动重写此前的未加密对象。

已有 MinIO 容器升级本配置时，先准备密钥文件，再运行上面的 `up` 和 `artifact-init`；
Compose 会重建 MinIO 容器并保留数据卷。BFF 和 Agent Server 继续使用 `AES256`，无需修改。

如果 volume 早于这套初始化脚本存在，初始化脚本不会重新执行。此时应先备份已有数据；
确认可以清空本地数据后，再显式执行 `docker compose -f compose.local.yml down -v`
并重新 `up`。不要在含有需要保留数据的环境执行 `down -v`。

## 3. 迁移 BFF 业务库

```bash
.venv/bin/dotenv -f .env.bff.local run -- .venv/bin/alembic upgrade head
.venv/bin/dotenv -f .env.bff.local run -- .venv/bin/alembic current
```

本地配置也关闭了自动建表，避免 BFF 与 Alembic 产生两套 schema 管理路径。

## 4. 启动持久化 Agent Server

在第一个终端运行：

```bash
.venv/bin/langgraph up \
  --config langgraph.local.json \
  --postgres-uri 'postgres://financeclaw:financeclaw-local@host.docker.internal:5432/financeclaw_agent?sslmode=disable' \
  --port 2024
```

不要用 `langgraph dev` 代替这一步来验证数据库持久化；`dev` 的 Agent Server
状态保存在本地 `.langgraph_api`，不会使用 `financeclaw_agent`。`langgraph up`
还会为 Agent Server 启动自己的 Redis。

首次运行会拉取基础镜像并构建 Agent Server，耗时会比后续运行长。出现 `Ready!` 后验证：

```bash
curl -fsS http://127.0.0.1:2024/ok
```

## 5. 启动 BFF

在第二个终端运行：

```bash
.venv/bin/uvicorn main:app \
  --env-file .env.bff.local \
  --host 127.0.0.1 \
  --port 8000 \
  --reload
```

验证 BFF 及全部依赖：

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
```

`/ready` 必须同时返回 `database`、`artifact_store`、`agent_server` 为 `true`。

## 6. 验证模型、LangSmith 和端到端会话

先单独验证 DeepSeek 的 tool calling、JSON 输出、治理工具执行和 LangSmith trace：

```bash
.venv/bin/dotenv -f .env.bff.local run -- \
  .venv/bin/python -m financeclaw.operations.provider_probe
```

成功输出中的 `trace_url` 应能打开 LangSmith trace。随后创建会话：

```bash
curl -fsS -X POST http://127.0.0.1:8000/v1/conversations \
  -H 'Authorization: Bearer local-only-token' \
  -H 'Content-Type: application/json' \
  -d '{}'
```

从响应取出 `conversation_id`，提交一轮：

```bash
curl -fsS -X POST \
  http://127.0.0.1:8000/v1/conversations/CONVERSATION_ID/turns \
  -H 'Authorization: Bearer local-only-token' \
  -H 'Idempotency-Key: local-turn-001' \
  -H 'Content-Type: application/json' \
  -d '{"message":"读取 AAPL 行情并说明数据时间"}'
```

从响应取出 `run_id`，轮询结果：

```bash
curl -fsS http://127.0.0.1:8000/v1/runs/RUN_ID \
  -H 'Authorization: Bearer local-only-token'
```

## 7. 停止

对前台 BFF 和 `langgraph up` 分别按 `Ctrl-C`，再停止基础设施：

```bash
docker compose -f compose.local.yml down
```

该命令保留 PostgreSQL 和 MinIO volumes。只有确认本地数据可以删除时才追加 `-v`。

## 常见问题

- Docker 报 daemon 不可用：先启动 Docker Desktop，再重试。
- `financeclaw_agent` 不存在：通常是旧 volume 跳过了初始化脚本，按第 2 步处理。
- Agent Server 容器连不上 PostgreSQL/MinIO：确认使用的是
  `.env.agent-server.local`，其中地址必须是 `host.docker.internal`。
- BFF 连不上 PostgreSQL/MinIO：确认使用的是 `.env.bff.local`，其中地址必须是
  `127.0.0.1`；如果本机设置了 HTTP(S) 代理，不要删除模板中的 `NO_PROXY/no_proxy`。
- LangSmith 401：检查 API key；若 key 跨多个 workspace，配置
  `LANGSMITH_WORKSPACE_ID`。
- `/ready` 返回 503：按响应的 `checks` 字段定位数据库、制品存储或 Agent Server。
