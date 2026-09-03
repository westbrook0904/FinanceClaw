# FinanceClaw

FinanceClaw 正在按 [`.redesign/`](.redesign/README.md) 从自研通用 Agent Harness 收敛为
“LangChain/LangGraph/LangSmith + 金融领域核心”。`.redesign/` 是当前唯一目标架构基线。

## 当前阶段

Stage 3 Long-term Memory 已成为默认入口，并在 Stage-2 会话主链上增加：

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

旧 `harness-runtime`、`harness-registry`、`harness-selection`、`harness-spi`、
`harness-plugin-local`、`harness-context`、`harness-memory`、`harness-policy`、通用
Provider/Capability/Context/Memory contracts 和示例 plugins 已删除。验证证据见
[Stage-3 验证记录](.redesign/stages/Stage-3-验证记录.md)。Stage-0 Spike 仅作为框架兼容性历史
切片保留，不再是产品入口。

## 环境

推荐用 conda 管理解释器，用 uv 把锁定依赖安装到同一个项目内环境：

```bash
conda create --yes --prefix .conda/envs/stage0 python=3.13 pip uv=0.12.9

UV_PROJECT_ENVIRONMENT="$PWD/.conda/envs/stage0" \
  .conda/envs/stage0/bin/uv sync \
  --all-extras --frozen \
  --python .conda/envs/stage0/bin/python
```

复制 `.env.stage3.example` 为 `.env`。DeepSeek 通过 OpenAI 协议接入时，核心配置为：

```dotenv
FINANCECLAW_MODEL=openai:deepseek-v4-pro
FINANCECLAW_PROVIDER_BASE_URL=https://api.deepseek.com
FINANCECLAW_PROVIDER_API_KEY=your-deepseek-api-key
```

Secret 只放 `.env` 或部署平台 Secret Manager，不要写入 Git 跟踪的 example 文件。

升级已有环境或部署生产前先执行 Alembic；生产使用 PostgreSQL 并关闭自动建表：

```bash
.conda/envs/stage0/bin/alembic upgrade head
```

## 运行

先启动内部 Agent Server：

```bash
.conda/envs/stage0/bin/langgraph dev --no-browser --no-reload --port 2024
```

再启动唯一产品入口 BFF：

```bash
.conda/envs/stage0/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

通过 `POST /v1/conversations` 创建会话，再在 `POST /v1/runs` body 中传入返回的
`conversation_id`。后续轮次复用同一 ID；原始消息、摘要与 Manifest 均由业务数据库持久化。
长期记忆由 Agent Server 的 LangGraph Store 持久化；生产部署需把 Agent Server Store 配置为
PostgreSQL-backed 实现。记忆写入会暂停为审批，调用 `/v1/runs/{run_id}/resume` 批准或拒绝。

本地离线 Agent Server 冒烟：

```bash
FINANCECLAW_OFFLINE_MODEL=true \
FINANCECLAW_DEBUG_FULL_IO=false \
FINANCECLAW_ENVIRONMENT=test \
LANGSMITH_TRACING=false \
  .conda/envs/stage0/bin/langgraph dev --no-browser --no-reload --port 2024

.conda/envs/stage0/bin/python -m financeclaw.application.server_smoke
```

跨 Agent Server 重启的 Conversation smoke 可分两次运行：

```bash
.conda/envs/stage0/bin/python -m financeclaw.application.conversation_smoke \
  --idempotency-key before-restart

# 重启 Agent Server，并把第一次输出的 conversation_id 传入
.conda/envs/stage0/bin/python -m financeclaw.application.conversation_smoke \
  --conversation-id <conversation_id> --idempotency-key after-restart
```

Stage-3 的 Memory HITL、跨 thread recall、Manifest 与 Audit 冒烟：

```bash
.conda/envs/stage0/bin/python -m financeclaw.application.memory_smoke
```

配置真实 Provider 与 LangSmith 后执行在线门禁：

```bash
.conda/envs/stage0/bin/python -m financeclaw.application.provider_probe
```

DeepSeek thinking 模型目前用 JSON mode 完成 structured output；默认原生 JSON Schema
`response_format` 和强制 `tool_choice` 在该兼容端点上可能返回 HTTP 400。

## 测试

```bash
.conda/envs/stage0/bin/python -m pytest -q
.conda/envs/stage0/bin/ruff check financeclaw financeclaw_spike harness-contracts/src \
  harness-events/src harness-trace/src tests pyproject.toml
.conda/envs/stage0/bin/ruff format --check financeclaw financeclaw_spike tests
```

配置 `LANGSMITH_API_KEY` 后，可幂等创建 Stage-3 的五个 Memory 回归样本：

```bash
.conda/envs/stage0/bin/python -m financeclaw.application.memory_eval_seed
```

## 目标架构

```text
FinanceClaw API / BFF
  → LangGraph Agent Server
      → LangChain Agent / Models / BaseTool / Middleware
      → MCP / Financial Services
      → PostgreSQL / Redis / Artifact Store
  → Conversation / Memory / Governance / Audit
  → LangSmith Trace / Evaluation
```

后续 Stage 按垂直切片推进，不恢复第二套 Runtime、Registry、Provider SPI 或 Plugin 生命周期。
