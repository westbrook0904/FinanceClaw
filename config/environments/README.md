# 环境配置

统一部署使用 `compose.yml` 和 `unified.env.example`。复制到 `.env` 后填写密钥；示例不得保存真实凭据。API、worker、integrations 的角色、数据库和内部地址由 Compose 注入，三个角色使用同一镜像及发布策略。

- development/test：允许静态产品 Bearer 认证和确定性离线模型。
- production：使用 `production.env.example` 的 OIDC、PostgreSQL、加密 S3、遥测和隐私策略；令牌由 Secret Manager 注入。
- `FINANCECLAW_INTEGRATION_SERVICE_TOKEN` 至少 32 字符；用于渠道入口和限定 Store namespace 的维护，不能操作原生运行。
- `FINANCECLAW_INTERNAL_API_URL` 只供 integrations 使用。API 的核心 SDK 固定为 `get_client(url=None, api_key=None)`。
- API 的 `N_JOBS_PER_WORKER=0`；worker 的并发必须为正，使用官方 queue entrypoint。

飞书默认关闭。启用时配置 APP_ID、APP_SECRET、ALLOWED_OPEN_IDS、SCOPES，并在生产使用 strict 模式。只有 integrations 建立 WebSocket，API 副本数不影响连接数量；默认运行一个开启渠道的 integrations 实例。通知和历史索引在独立任务中执行。

Ziwei 仍为开发/测试候选，需要显式约定、HMAC 密钥和隐私设置，见 [领域说明](../../docs/operations/ziwei-agent.md)。所有改变发布指纹的配置应在 API 与 worker 间一致。
