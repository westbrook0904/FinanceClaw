# FinanceClaw

FinanceClaw 基于 LangChain、LangGraph Agent Server 与 LangSmith，提供金融场景的会话、受治理工具、人工审批、上下文、记忆、制品与审计。

当前架构由 BFF 与 Agent Server 两个服务组成：BFF 负责 start、人工 resume、cancel、授权和永久聊天记录；顶层 ReAct 通过 Tool 调用领域 Agent 或 Workflow subgraph。一个业务 Turn 只有一个根执行，Worker 不创建独立 thread/run。当前只注册 finance_agent_v1_6_0 根图。

BFF 的 Webhook 接收器和后台结果核对独立于客户端连接。最终答案、运行状态、审计和通知意图同事务落库；BFF 内置飞书发送器按持久责任交付。未知提交不会自动换 ID 重发。

产品仅通过 Conversation 与 message-only Turn 发起执行，查询和 SSE 只读。/tool、/agent、/workflow 是调用偏好；人工回复统一经 interaction responses。代码目录与依赖见 [包结构](docs/architecture/package-layout.md)，实施契约见 [Stage 8 Hotfix](.redesign/stages/stage-8-hotfix-实施方案.md)。

项目尚未上线：数据库迁移为当前的 0001_initial，不维护未发布 schema 的升级兼容。使用新空开发库初始化，已有本机数据库不会自动删除或重置。

[飞书交互卡片实现](.redesign/stages/Feishu-交互卡片适配实施方案.md) · [BFF 运行手册](docs/operations/bff-run-control.md) · [飞书通知](docs/operations/notifications.md) · [紫微候选](docs/operations/ziwei-agent.md)

Stage 9 实现：[Stage 9：上下文与记忆优化实施方案](.redesign/stages/stage-9-上下文与记忆优化实施方案.md)，依据[当前系统评估](docs/architecture/memory-assessment-2026-09-10.md)。实现与验证见[Stage 9 验收记录](.redesign/stages/stage-9-实现与验证.md)。

## 环境

推荐用 conda 管理解释器，用 uv 把锁定依赖安装到同一个项目内环境：

```bash
conda create --yes --prefix .conda/envs/financeclaw python=3.13 pip uv=0.12.9

UV_PROJECT_ENVIRONMENT="$PWD/.conda/envs/financeclaw" \
  .conda/envs/financeclaw/bin/uv sync \
  --all-extras --frozen \
  --python .conda/envs/financeclaw/bin/python
```

本地从 `config/environments/development.env.example` 生成 `.env`；生产从
`config/environments/production.env.example` 生成部署配置，并由 Secret Manager 注入真实凭据。
DeepSeek 通过 OpenAI 协议接入时，核心配置为：

```dotenv
FINANCECLAW_MODEL=openai:deepseek-v4-pro
FINANCECLAW_PROVIDER_BASE_URL=https://api.deepseek.com
FINANCECLAW_PROVIDER_API_KEY=your-deepseek-api-key
```

默认上下文容量上限为 800,000 token；原生 state 保留近期消息，达到阈值后摘要旧 Turn，保护当前 Turn 及最近 4 个已完成 Turn。画像直接读 Store，长期事件按 Turn 语义召回一次，历史原文按需分页回读；工具结果清理前归档。配置与后台角色见[上下文与记忆运维](docs/operations/context-budget.md)。

Secret 只放 `.env` 或部署平台 Secret Manager，不要写入 Git 跟踪的 example 文件。

飞书一期使用企业自建应用。在飞书后台启用机器人、WebSocket 事件订阅和
`im.message.receive_v1` 和 `card.action.trigger`，并授予消息收发与 CardKit 创建/更新权限；权限变化后需重新发布、安装应用。
BFF 配置至少包含：

```dotenv
FINANCECLAW_FEISHU_ENABLED=true
FINANCECLAW_FEISHU_APP_ID=cli_xxx
FINANCECLAW_FEISHU_APP_SECRET=<from-secret-manager>
FINANCECLAW_FEISHU_ALLOWED_OPEN_IDS='["ou_allowed_user"]'
FINANCECLAW_FEISHU_SCOPES='["market:read","tools:read","artifacts:read","memory:read"]'
FINANCECLAW_FEISHU_MAX_CONCURRENCY=8
FINANCECLAW_FEISHU_SECURITY_MODE=audit
```

生产启用时必须把 security mode 切换为 `strict`，且同一部署只允许一个 BFF 实例开启 Channel。

使用空数据库执行初始迁移；生产使用 PostgreSQL 并关闭自动建表：

```bash
.conda/envs/financeclaw/bin/alembic upgrade head
```

## 运行

需要在本机同时启动 Docker PostgreSQL、共享 MinIO、持久化 Agent Server、BFF，
并接入真实 DeepSeek 与 LangSmith 时，直接按[本地完整链路启动手册](docs/operations/local-full-stack.md)
执行；对应配置模板是 `config/environments/local-*.env.example`，本地基础设施定义是
`compose.local.yml`。

先启动内部 Agent Server：

```bash
.conda/envs/financeclaw/bin/langgraph dev --no-browser --no-reload --port 2024
```

按 [BFF 运行手册](docs/operations/bff-run-control.md) 配置 Webhook，
BFF 就绪后直接受理。执行与卡片投递循环随 BFF 启停：

```bash
.conda/envs/financeclaw/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

通过 `POST /v1/conversations` 创建会话，再调用
`POST /v1/conversations/{conversation_id}/turns`，请求体只传 `message`。后续轮次复用同一 ID；
原始问答与 Manifest 由业务数据库持久化；工作消息/摘要由原生 checkpoint 保存，画像和事件由原生 Store 保存。需要明确表达调用偏好时，把
`/tool ...`、`/workflow ...` 或 `/agent ...` 直接写入 `message`，不要在请求体中传 Target。
长期记忆由 Agent Server 的 LangGraph Store 持久化；生产部署需把 Agent Server Store 配置为
PostgreSQL-backed 实现。记忆写入会暂停为审批，通过 `/v1/interactions/{interaction_id}/responses` 批准或拒绝。

隔离的 BFF／Agent Server 原生验证：

```bash
.venv/bin/python -m experiments.stage8_hotfix.hf2_native --report /tmp/bff-native.json
.venv/bin/python -m experiments.stage8_hotfix.hf2_native --scenario hitl --report /tmp/bff-hitl.json
```

探针启动自己的临时服务与数据库，覆盖子图调用、人工恢复、Webhook 与最终聊天记录。

配置真实 Provider 与 LangSmith 后执行在线门禁：

```bash
.conda/envs/financeclaw/bin/python -m financeclaw.operations.provider_probe
```

DeepSeek thinking 模型目前用 JSON mode 完成 structured output；默认原生 JSON Schema
`response_format` 和强制 `tool_choice` 在该兼容端点上可能返回 HTTP 400。

## 测试

```bash
.conda/envs/financeclaw/bin/python -m pytest -q
.conda/envs/financeclaw/bin/ruff check financeclaw tests scripts
.conda/envs/financeclaw/bin/ruff format --check financeclaw tests scripts
.conda/envs/financeclaw/bin/python scripts/generate_sbom.py
.conda/envs/financeclaw/bin/uv export --frozen --no-dev --no-emit-project \
  --format requirements-txt --output-file build/production-requirements.txt
.conda/envs/financeclaw/bin/pip-audit --strict --require-hashes --disable-pip \
  --requirement build/production-requirements.txt
```

配置 `LANGSMITH_API_KEY` 后，可创建带版本名的 Stage-5 发布回归数据集：

```bash
.conda/envs/financeclaw/bin/python -m financeclaw.evaluation.publish_dataset \
  --name financeclaw-stage5-regression-v1
```

## 目标架构

```text
FinanceClaw API / BFF
  → LangGraph Agent Server
      → LangChain Agent / Models / BaseTool / Middleware
      → MCP / Financial Services
      → PostgreSQL / Redis / Artifact Store
  → Conversation / Memory / Published Workflows / Governance / Audit
  → LangSmith Trace / Evaluation
```

真实上线仍须完成组织级许可证/数据驻留评审、真实容量与故障注入、恢复演练和安全评审；清单见
[`docs/operations/release-checklist.md`](docs/operations/release-checklist.md)。任何后续功能都不得恢复
第二套 Runtime、Registry、Provider SPI 或 Plugin 生命周期。
