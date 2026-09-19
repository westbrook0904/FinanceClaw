# 环境配置从哪里开始

首次运行选择 [`unified.env.example`](unified.env.example) 与 [`memory.env.example`](memory.env.example)，完整命令见[本地启动](../../docs/operations/local-full-stack.md)。示例中的密码、Token、URL 和 Secret Manager 引用都是占位符；不要直接当作可用配置，也不要把填写后的私有环境文件提交。

## 文件分工

| 文件 | 用途 | 使用注意 |
|---|---|---|
| `unified.env.example` | 根 Compose 的本地四角色部署 | 复制到 `.env`；容器角色与 DSN 由 Compose 设置 |
| `memory.env.example` | 独立记忆工作进程 | 复制到 `.env.memory`，只放它所需的配置和密钥 |
| `development.env.example` | 单进程开发配置参考 | 不是完整持久部署清单；先确认数据库与原生运行时入口 |
| `test.env.example` | 测试隔离配置参考 | SQLite / 离线模型不代表生产行为 |
| `staging.env.example` | 预发布身份、存储、遥测配置参考 | 替换所有服务地址和凭据，单独验证真实依赖 |
| `production.env.example` | 生产约束清单 | 需要 OIDC、PostgreSQL、加密 S3、遥测；结合[生产手册](../../docs/operations/production-runbook.md)使用 |

根 Compose 的 `env_file` 默认为 `.env`，记忆角色改用 `.env.memory`。`FINANCECLAW_ENV_FILE` / `FINANCECLAW_MEMORY_ENV_FILE` 可覆盖对应文件路径；部署脚本的 `--env-file` 同时选择 Compose 插值文件和前三个业务角色的环境文件，不会替换记忆文件路径。

## 四个角色怎样共享配置

| 角色 | 关键配置 / 凭据 | 责任 |
|---|---|---|
| API | 产品认证、应用库、发布策略、原生运行时配置 | 受理和核对；固定 `N_JOBS_PER_WORKER=0` |
| Worker | 与 API 一致的模型/工具/领域发布配置 | 官方 queue entrypoint 执行原生图；并发为正 |
| integrations | 内部 API 服务令牌、数据库、可选飞书凭据 | 渠道、通知、Store 索引和删除 |
| memory_worker | 有限数据库身份、记忆模型密钥、允许的数据分级/区域 | 记忆提取与整理；不持有产品、渠道、管理员或原生 Store 凭据 |

`.env` 与 `.env.memory` 中的 `MEMORY_POSTGRES_PASSWORD` 必须一致，至少 24 个字符。迁移入口创建/配置 `financeclaw_memory` 权限，记忆入口使用该身份构造 DSN。`FINANCECLAW_INTEGRATION_SERVICE_TOKEN` 至少 32 个字符，仅用于内部渠道入口与限定 Store namespace，不能操作原生运行。

API 的核心 SDK 固定使用 `get_client(url=None, api_key=None)`；`FINANCECLAW_INTERNAL_API_URL` 服务于 integrations 和显式维护 CLI，不代表仍有独立 BFF → AgentServer HTTP 执行层。

## 模型与索引

供应商、模型名、容量、超时和默认别名统一在 [`config/models.toml`](../models.toml) 定义，`FINANCECLAW_MODEL_CONFIG_PATH` 选择文件。Agent、摘要和记忆任务未单独绑定时使用 `defaults.model`。每个进程只注入自己使用的 `api_key_env` 对应密钥；API 可冻结记忆模型档案，而无需持有专用记忆模型密钥。

初次验证可让 `.env` 与 `.env.memory` 同时保留 `FINANCECLAW_OFFLINE_MODEL=true`；切换真实记忆模型时须核对两侧档案、离线开关、允许数据级别和区域一致。模型变更会影响发布/任务指纹，不能静默接管不匹配的旧任务。详情见[模型配置](../../docs/operations/model-configuration.md)。

embedding 独立于聊天模型配置。模型实际输出维度、`FINANCECLAW_EMBEDDING_DIMENSIONS`、[`langgraph.json`](../../langgraph.json) 与原生 Store 向量列必须一致；当前仓库默认 1024。更换配置不会自动迁移已有向量列，见 [Outbox 排查](../../docs/operations/memory-outbox.md)。

## 可选能力的当前状态

| 能力 | 仓库/统一示例状态 | 启用前还需配置 |
|---|---|---|
| Skills | 统一示例开启 | 受发布 manifest、Agent 绑定和身份权限约束，见 [Skills](../../docs/operations/skills.md) |
| 飞书 | 关闭 | APP_ID、APP_SECRET、open_id allowlist、scopes 和平台事件/CardKit 权限；生产 `strict` |
| 紫微 | 关闭，development/test 候选 | 明确 convention、HMAC 密钥、隐私配置与领域权限，见[紫微手册](../../docs/operations/ziwei-agent.md) |
| Taibu MCP | 关闭 | 可叠加 `compose.taibu.yml`，身份需 `taibu:read`；八字还需出生资料隐私配置 |
| RollingGo 酒店 MCP | **`config/mcp.toml` 当前开启** | `FINANCECLAW_ROLLINGGO_API_KEY` 与身份 `travel:read`；不用时显式设 `enabled=false` |
| RollingGo 机票 MCP | 关闭 | 固定工具契约、服务密钥、任务数据级别与身份权限 |

模型离线开关不会关闭 MCP 或飞书。酒店工具的根任务数据级别允许策略见 [MCP 接入手册](../../docs/operations/mcp.md)；不要只配置 API key 而遗漏 scopes 或数据级别。`uv run --frozen python scripts/deploy.py` 会准备已启用 MCP 的缺失定义再构建部署，更新现有定义时使用 `--refresh-mcp`。

飞书仅由 integrations 建立 WebSocket，默认一个开启渠道的实例；API 扩容不增加连接数。Taibu 的启动、Host 端口配置与真实验证边界见 [Taibu 手册](../../docs/operations/taibu-mcp.md)。

## 修改配置后的检查

1. 先识别变化是否影响模型档案、工具契约或发布指纹，并处理在途任务。
2. 用 `docker compose config --quiet` 检查配置，不将展开的 secret 打印到日志。
3. 镜像内 TOML/契约或源码变化后重新构建；容器环境变化后重新创建相关角色，单纯 `restart` 不会载入新的容器环境。
4. 检查 API ready、三个后台角色健康及相关 outbox，再执行本次变更对应的真实功能验证。
