# 环境配置

统一部署使用 `compose.yml`。将 `unified.env.example` 复制到 `.env`、`memory.env.example` 复制到 `.env.memory` 后填写凭据。四个角色使用同一镜像；memory_worker 仅加载独立环境文件和有限权限数据库身份，不接收业务管理员、产品、渠道或原生 Store 凭据。两个文件中的 `MEMORY_POSTGRES_PASSWORD` 必须一致。使用 `FINANCECLAW_MEMORY_ENV_FILE` 可以指定另一个记忆环境文件路径。

模型服务商、名称、容量及默认别名统一在 `config/models.toml` 声明，环境文件通过 `FINANCECLAW_MODEL_CONFIG_PATH` 指定同一文件，并分别注入本进程需要的模型密钥。Agent、摘要和记忆任务未单独指定时都使用 `defaults.model`，详见[统一模型配置](../../docs/operations/model-configuration.md)。

- development/test：允许静态产品 Bearer 认证和确定性离线模型。
- production：使用 `production.env.example` 的 OIDC、PostgreSQL、加密 S3、遥测和隐私策略；令牌由 Secret Manager 注入。
- `FINANCECLAW_INTEGRATION_SERVICE_TOKEN` 至少 32 字符；用于渠道入口和限定 Store namespace 的维护，不能操作原生运行。
- `FINANCECLAW_INTERNAL_API_URL` 只供 integrations 使用。API 的核心 SDK 固定为 `get_client(url=None, api_key=None)`。
- API 的 `N_JOBS_PER_WORKER=0`；worker 的并发必须为正，使用官方 queue entrypoint。
- memory_worker 的模型名、服务地址、离线开关、容量及允许处理的数据分级/区域应与 API 冻结的记忆档案一致，API 不需要持有其专用模型密钥；变更发布策略需统一发布。配置与操作命令见[上下文与异步记忆](../../docs/operations/context-budget.md)。

飞书默认关闭。启用时配置 APP_ID、APP_SECRET、ALLOWED_OPEN_IDS、SCOPES，并在生产使用 strict 模式。只有 integrations 建立 WebSocket，API 副本数不影响连接数量；默认运行一个开启渠道的 integrations 实例。通知和历史索引在独立任务中执行。

Ziwei 仍为开发/测试候选，需要显式约定、HMAC 密钥和隐私设置，见 [领域说明](../../docs/operations/ziwei-agent.md)。所有改变发布指纹的配置应在 API 与 worker 间一致。

Taibu MCP 默认关闭。叠加 `compose.taibu.yml` 可启用内网黄历、八字服务；API 与 Worker 同步获得配置，身份仍需单独授予 `taibu:read`。出生资料要求关闭完整 I/O 调试并隐藏追踪输入输出，具体启动、验收和回退命令见 [Taibu 运行说明](../../docs/operations/taibu-mcp.md)。
