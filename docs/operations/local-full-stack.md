# 统一镜像的本地完整链路

使用 [统一环境示例](../../config/environments/unified.env.example) 和根目录 `compose.yml`。默认项目名 `financeclaw-stage11`，使用新数据库卷、Redis 卷和共享制品卷；原有 `financeclaw-local` 容器及数据不会被修改。

1. 将示例复制为 `.env`，将 `config/environments/memory.env.example` 复制为 `.env.memory`，填写各自凭据。
2. 执行 `uv run --frozen python scripts/deploy.py`：自动准备 MCP 定义、构建共用镜像，并沿用 Compose 迁移和服务启动流程。
3. 检查 `http://127.0.0.1:8000/v1/health/ready`；使用产品 Bearer Token 创建 Conversation 与 Turn。

更新 MCP 定义时加 `--refresh-mcp`，只准备文件时加 `--prepare-only`。其他环境文件和 Compose 组合参数见 [MCP 部署说明](mcp.md#发布到-docker)。

默认离线模型是确定性验证模型，不代表真实行情或模型回答质量。使用真实模型时设置 `FINANCECLAW_OFFLINE_MODEL=false`，在 `config/models.toml` 中配置供应商、模型及默认别名，并通过环境文件注入 `api_key_env` 引用的密钥。API 与各 Worker 应读取同一份 TOML，发布策略、领域开关保持一致，详见[统一模型配置](model-configuration.md)。

本地默认制品目录挂载到所有角色的 `/data/artifacts`。生产使用 `production.env.example` 的 S3 加密与 OIDC 设置；S3 endpoint 及遥测地址须存在并通过主机 allowlist。Compose 不自动创建生产对象桶、OIDC 或遥测服务。

只有一个 integrations 实例开启飞书 WebSocket。配置好飞书事件、CardKit 权限、APP_ID、APP_SECRET、open_id allowlist 与 scopes 后重启对应角色。API 不建立飞书连接，原生 Worker 不启动通知发送器。

停止本次项目用 `docker compose stop`；不要为日常重启删除卷。未上线 schema 变更需显式选择另一个空库或新项目卷，不能让旧本地库自动迁移或重置。详细故障语义见 [Turn 运行手册](turn-control.md)。
