# FinanceClaw

FinanceClaw 正在按 [`.redesign/`](.redesign/README.md) 从自研通用 Agent Harness 收敛为
“LangChain/LangGraph/LangSmith + 金融领域核心”。`.redesign/` 是当前唯一目标架构基线。

## 当前阶段

Stage 5 Production Hardening 已完成仓库内可自动验证的生产加固；对外接口保持“根会话统一进入
顶层 Agent”：

- 产品写入口只有 Conversation 创建和 message-only Turn 提交，不接受 Agent、Tool 或 Workflow Target；
- `finance_agent` 使用 ReAct 判断直接回答、Tool Calling、Workflow handoff 或领域 Agent delegation；
- `/tool <id>`、`/workflow <id>`、`/agent <id>` 是调用偏好，不是身份或权限；
- `/tool` 已支持 Pydantic Schema 校验、缺参提槽、单 Tool 候选约束和执行时二次授权；
- 原直接 Tool/Workflow/Run 创建路由从 OpenAPI 隐藏，并仅允许 `internal:invoke` 服务身份；
- Workflow 与领域 Agent 已作为受治理的 delegation Tool 暴露给顶层 Agent，并使用 typed handoff、
  独立 child thread/run 和永久父子映射执行。

Stage 5 新增的生产能力包括：

- 生产 BFF 使用 OIDC/JWT issuer、audience、时效和非对称算法校验，从可信 claims 生成
  tenant、subject 与 scopes；静态 token 只允许本地开发；
- Provider、OIDC JWKS 与内部 Agent Server 的出站目标启动时按 allowlist 校验；客户端不跟随
  健康检查重定向，并为外部调用设置超时；
- OpenTelemetry 关联 HTTP、数据库与 Agent Server 调用，结构化日志默认脱敏；正式 Audit 不由
  trace 或普通日志替代；
- 每条永久 Audit 与有界 Outbox 事件在同一数据库事务落盘，异步 publisher 使用租约、退避与
  dead-letter；
- Artifact Store 支持 S3 兼容后端、强制 SSE、内容 checksum，以及不暴露原始身份的
  tenant/subject key；
- `/ready` 组合检查业务 PostgreSQL、Artifact Store 与 Agent Server，lifespan 负责数据库关闭和
  OTel flush；
- `evals/stage5-regression-v1.json` 覆盖工具/补槽/委派/策略/记忆/金融时效/恢复/注入/租户/Provider
  故障，关键用例与总分作为发布门禁；
- 生产镜像、部署基线、SBOM、依赖漏洞扫描 CI，以及发布、故障、灾备和数据主体请求 Runbook；
- 旧 `financeclaw_spike`、`harness-contracts`、`harness-events`、`harness-trace` 与相关旧测试、
  构建声明已删除，不再保留双 Runtime。

Stage 4 已交付的固定流程能力继续保留：

- 不可变、启动期装配的 `WorkflowCatalog`，固定 workflow、graph revision、ModelProfile、Tool
  版本、审批点和超时策略；
- 首个真实流程 `portfolio_review@1.0.0`：校验输入、读取有来源与时间戳的行情、检查新鲜度、
  确定性计算组合集中度、审批后发布报告制品；
- Workflow 独占 Agent Server thread，BFF 永久保存业务 run、thread、server run、发布版本、输入 hash、
  审批和 artifact 映射；
- 审批使用 LangGraph interrupt/resume，权限、owner、原始参数 hash 和过期时间均在恢复前复验；
- `portfolio_review` 可由顶层 Agent 或 `/workflow portfolio_review` 指令选中，BFF 再次校验
  scope、版本与输入 Schema 后创建独立 child run；
- `market_research_agent@1.0.0` 是首个只读领域 Agent，只获得行情读取 Tool，不拥有根 Conversation；
- `delegations` 永久记录 parent turn/run、child thread/run/server run、目标版本、参数 hash、状态和结果；
- child Workflow 的审批沿用原 interrupt/resume，完成后以 `DelegationResult` 恢复父 Agent 汇总；
- 报告写入使用 run/node 幂等键，State/checkpoint 只保留有界结构化数据和 artifact 引用；
- Alembic `0003_stage4`/`0004_delegations`、完整生命周期 Audit 和 LangSmith 五类回归样本。

此前 Stage-2/3 能力继续保留：

- 永久、append-only 的 Conversation Journal，以及 Conversation/Thread/AgentProfile 固定映射；
- 基于 token budget 的最近原文、分段/分层摘要和相关古老历史选择；
- 每次模型调用永久保存的 `ModelContextManifest` 和 development 完整 Prompt 调试；
- 大 Tool Result 的 Artifact offload、hash 校验和 owner/scope 隔离读取；
- LangGraph Store 上的 preference/goal/constraint/decision_note 长期记忆；
- `propose_memory` → HITL → `confirm_memory` 的受控写入，以及 supersede/revoke/delete 生命周期；
- 按可信 tenant/subject namespace 的跨会话召回、独立 token 预算和 data-only Prompt 区域；
- Memory 的版本化 Manifest 引用、LangSmith recall/write span 与永久 Audit；
- SQLAlchemy 业务模型与 Alembic 迁移，支持未完成 turn 对账和跨 Agent Server 重启继续；
- FastAPI BFF 的 Conversation 创建、查询、消息查询和持久化 run 编排；
- Stage-1 的 Tool 治理、审批、retry/fallback、SSE、Audit 与 DeepSeek OpenAI 协议继续保留。

全部旧 Harness Runtime、Registry、Selection、SPI、Context、Memory、Policy、Trace、Events、
Contracts 空壳和示例实现均已从生产构建删除。验证证据见
[Stage-5 验证记录](.redesign/stages/Stage-5-验证记录.md)。

## 代码结构

正式代码按企业级模块化单体组织：`interfaces` 承接 HTTP 协议，`application` 协调用例，
`modules` 聚合领域模块，`orchestration` 承载 Agent/Graph/Tool 运行时，`infrastructure` 提供数据库、
外部客户端、LLM、安全和观测适配，`kernel` 保存稳定共享契约，`operations` 只保存运维命令。
完整职责及依赖规则见 [包结构设计](docs/architecture/package-layout.md)。

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
`.env.stage5.example` 生成部署配置，并由 Secret Manager 注入真实凭据。DeepSeek 通过 OpenAI
协议接入时，核心配置为：

```dotenv
FINANCECLAW_MODEL=openai:deepseek-v4-pro
FINANCECLAW_PROVIDER_BASE_URL=https://api.deepseek.com
FINANCECLAW_PROVIDER_API_KEY=your-deepseek-api-key
```

Secret 只放 `.env` 或部署平台 Secret Manager，不要写入 Git 跟踪的 example 文件。

升级已有环境或部署生产前先执行 Alembic；生产使用 PostgreSQL 并关闭自动建表：

```bash
.conda/envs/financeclaw/bin/alembic upgrade head
```

## 运行

先启动内部 Agent Server：

```bash
.conda/envs/financeclaw/bin/langgraph dev --no-browser --no-reload --port 2024
```

再启动唯一产品入口 BFF：

```bash
.conda/envs/financeclaw/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

通过 `POST /v1/conversations` 创建会话，再调用
`POST /v1/conversations/{conversation_id}/turns`，请求体只传 `message`。后续轮次复用同一 ID；
原始消息、摘要与 Manifest 均由业务数据库持久化。需要明确表达调用偏好时，把
`/tool ...`、`/workflow ...` 或 `/agent ...` 直接写入 `message`，不要在请求体中传 Target。
长期记忆由 Agent Server 的 LangGraph Store 持久化；生产部署需把 Agent Server Store 配置为
PostgreSQL-backed 实现。记忆写入会暂停为审批，调用 `/v1/runs/{run_id}/resume` 批准或拒绝。

本地离线 Agent Server 冒烟：

```bash
FINANCECLAW_OFFLINE_MODEL=true \
FINANCECLAW_DEBUG_FULL_IO=false \
FINANCECLAW_ENVIRONMENT=test \
LANGSMITH_TRACING=false \
  .conda/envs/financeclaw/bin/langgraph dev --no-browser --no-reload --port 2024

.conda/envs/financeclaw/bin/python -m financeclaw.operations.server_smoke
```

跨 Agent Server 重启的 Conversation smoke 可分两次运行：

```bash
.conda/envs/financeclaw/bin/python -m financeclaw.operations.conversation_smoke \
  --idempotency-key before-restart

# 重启 Agent Server，并把第一次输出的 conversation_id 传入
.conda/envs/financeclaw/bin/python -m financeclaw.operations.conversation_smoke \
  --conversation-id <conversation_id> --idempotency-key after-restart
```

Stage-3 的 Memory HITL、跨 thread recall、Manifest 与 Audit 冒烟：

```bash
.conda/envs/financeclaw/bin/python -m financeclaw.operations.memory_smoke
```

Stage-4 固定 Workflow 的独立 thread、审批、报告制品、Audit 与进程内业务恢复冒烟：

```bash
.conda/envs/financeclaw/bin/python -m financeclaw.operations.workflow_smoke
```

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
