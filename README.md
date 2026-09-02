# FinanceClaw

FinanceClaw 正在按 [`.redesign/`](.redesign/README.md) 从自研通用 Agent Harness 收敛为
“LangChain/LangGraph/LangSmith + 金融领域核心”。`.redesign/` 是当前唯一目标架构基线；
旧 Harness 代码在 Stage 0 保留，仅用于回归，不被新 Spike 引用。

## 当前阶段

Stage 0 Framework Compatibility Spike 已实现以下隔离切片：

- Python 3.13 + `uv.lock`；
- LangChain `create_agent` 与 OpenAI Provider Integration；
- READ `BaseTool` + `ToolRetryMiddleware`；
- WRITE `BaseTool` + `HumanInTheLoopMiddleware` + interrupt/resume；
- trusted runtime context 下的动态 Tool 过滤；
- LangGraph thread/checkpoint/stream 与 Agent Server graph；
- stateless MCP server、MCP → `BaseTool` 转换与本地治理覆盖；
- LangSmith 自动调用链、自定义 context run、完整开发 I/O 日志与凭证脱敏；
- PostgreSQL checkpointer、Redis Store 与 worker/graph 重建恢复探针。

实现位于 `financeclaw_spike/`，不依赖 `harness-runtime`、`harness-registry`、
`harness-spi` 或其他旧执行抽象。验证状态和版本差异见
[Stage-0 验证记录](.redesign/stages/Stage-0-验证记录.md)。

## Conda 环境

推荐用 Miniforge/conda 管理解释器，用 uv 把项目锁定依赖安装进同一个 conda 环境：

```bash
conda create --yes --prefix .conda/envs/stage0 python=3.13 pip uv=0.12.9

UV_PROJECT_ENVIRONMENT="$PWD/.conda/envs/stage0" \
  .conda/envs/stage0/bin/uv sync \
  --all-extras --frozen \
  --python .conda/envs/stage0/bin/python
```

也可使用 `environment.stage0.yml` 创建等价的命名环境。conda 环境、`.env`、IDE 文件和
Agent Server 本地状态均已从 Git 与 Docker build context 排除。

## 测试

不依赖外部凭证的确定性测试：

```bash
.conda/envs/stage0/bin/python -m pytest -q
.conda/envs/stage0/bin/ruff check financeclaw_spike tests/stage0 pyproject.toml
.conda/envs/stage0/bin/ruff format --check financeclaw_spike tests/stage0
```

本地 Agent Server：

```bash
FINANCECLAW_SPIKE_OFFLINE_MODEL=true \
FINANCECLAW_SPIKE_DEBUG_FULL_IO=false \
FINANCECLAW_SPIKE_ENVIRONMENT=test \
LANGSMITH_TRACING=false \
  .conda/envs/stage0/bin/langgraph dev --no-browser --no-reload

.conda/envs/stage0/bin/python -m financeclaw_spike.server_smoke
```

真实 Provider、LangSmith、PostgreSQL 与 Redis 验证前，复制 `.env.stage0.example` 为 `.env`
并填写本地 Secret。不要提交 `.env`：

```bash
.conda/envs/stage0/bin/python -m financeclaw_spike.probe provider
.conda/envs/stage0/bin/python -m financeclaw_spike.probe mcp

docker compose -p financeclaw-stage0 -f compose.stage0.yml up -d --wait
.conda/envs/stage0/bin/python -m financeclaw_spike.probe services
docker compose -p financeclaw-stage0 -f compose.stage0.yml down
```

Agent Server 镜像构建：

```bash
.conda/envs/stage0/bin/langgraph build -t financeclaw-stage0:spike
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

后续 Stage 必须先满足当前阶段门禁，再按垂直切片切换入口并删除对应旧模块，不长期维护双
Runtime 或兼容层。
