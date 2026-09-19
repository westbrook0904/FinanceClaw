# 生产部署与故障处置

本文描述当前统一 AgentServer 架构的发布要求与操作顺序，不是生产验收结论。首次理解各进程与健康检查可先读[本地启动](local-full-stack.md)；发布前逐项填写[发布检查表](release-checklist.md)。

## 网络与身份边界

对外入口是统一 `api` 的产品 `/v1/*` 路由。产品 API 与原生 AgentServer 同进程，但使用不同授权边界：产品用户不能直接操作原生 thread/run，integrations 服务身份只能调用限定的内部渠道入口与 Store namespace。Ingress 应明确列出允许的产品路由，内部渠道和原生资源保留在私网；不要依赖 URL 前缀代替认证。

同一镜像部署四个角色：`api` 受理并核对 Turn，`worker` 执行原生图，`integrations` 处理渠道、通知和索引，`memory_worker` 提取并整理记忆。API 的 `N_JOBS_PER_WORKER=0`，原生 Worker 的值必须为正。PostgreSQL、Redis、Artifact Store 和内部端点均在私网；默认只运行一个启用飞书 WebSocket 的 integrations 实例。

OIDC 验证后的 claims 决定 tenant、subject 和 scopes，请求体不能选择身份。`memory_worker` 不受理用户请求，使用单独的数据库身份和环境文件，不应获得产品、渠道、原生 Store 或业务管理员凭据。

## 发布前准备

1. 以 [production.env.example](../../config/environments/production.env.example) 为配置清单，由 Secret Manager 注入实际值。模板中的域名、桶名和 secret 引用不能直接使用。
2. 配置 HTTPS OIDC issuer / JWKS、audience 和非对称算法；生产禁止开发 Bearer Token、离线模型和完整 I/O 调试。
3. 准备应用库与原生库、Redis、加密 S3、OTel trace / metric 端点；供应商与基础设施主机进入相应 allowlist。根 Compose 是本地示例，不会代建生产 OIDC、对象桶或遥测服务。
4. 核对 `config/models.toml`、MCP 固定定义、Skill manifest、领域开关与权限。酒店 MCP 当前在仓库配置中已启用；不用的服务要显式关闭。紫微仍限制 development/test，不能作为生产已验收功能发布。
5. 固定镜像摘要、源码提交、模型/策略/契约版本和数据库迁移版本；API 与 Worker 必须匹配，记忆 Worker 的模型档案必须匹配 API 冻结的档案。

## 发布顺序与就绪条件

1. 备份数据库及 Artifact，并核对恢复方案。新部署先使用空应用库执行一次性迁移；当前 `deploy/migrate.py` 执行 Alembic 后配置记忆身份。原生库交给 AgentServer 初始化，不向其中执行应用迁移。
2. 已有库发布前先检查 schema 差异和专用升级脚本。当前仓库使用持续更新的初始迁移，`alembic upgrade head` 不能保证为所有旧开发库补齐字段；禁止用自动删库或降级迁移替代明确方案。
3. 启动 API，确认 `/v1/health/live` 响应和 `/v1/health/ready` 返回 `{"ready":true}`；再检查 Worker、integrations、memory_worker 自身健康。
4. 使用隔离测试主体验证会话受理、幂等重试、澄清/审批、取消、重启恢复和最终 Journal。按启用功能增加真实模型、MCP、对象存储与飞书验收。
5. 逐步放量，观察 API 延迟、原生队列与执行时长、blocked/uncertain Turn、模型/工具预算、通知与索引积压、记忆死信和 `purge_pending`。

就绪检查证明当前关键依赖可用，不证明供应商回答质量、飞书客户端交互或生产容量已达标。历史探针的适用范围见[实验说明](../../experiments/stage10/README.md)，当次发布需记录自己的镜像和验证结果。

## 发布失败与回退

先停止新请求受理并核对在途任务；未知提交要沿原 command 身份对账，不能创建新 native run 作为替代。保留 Journal、outbox 和通知投递的原始操作键，防止重复外部效果。

只有旧镜像与当前 schema、工具契约和已冻结任务版本仍匹配时，才可以回退应用镜像。影响发布指纹的开关或模型变更不能直接恢复旧任务；先排空，或保留能够处理旧版本任务的发布。数据库恢复需要单独的停写、备份和对账方案，不执行未经验证的 downgrade。

## 故障定位

| 现象 | 检查与处置 |
|---|---|
| Provider 不可用 | 查看明确的依赖错误、调用预算与 Provider 状态；有界重试耗尽后保留失败结果，不能声称动作已完成 |
| API readiness 503 | 从最早日志检查应用库、Artifact、schema、原生 SDK 和 Turn 后台责任；暂停导入新流量 |
| Worker 重启或失联 | 核对原生队列与运行状态，由持久 runtime 恢复；API 的状态核对不能替代图执行 |
| Turn `blocked` / `uncertain` | 按 [Turn 手册](turn-control.md) 判断授权、发布冲突或发送结果未知，保留原操作身份 |
| Outbox 积压或死信 | 检查负责该 destination 的进程，修复下游后使用受控重放/重建；不直接清零预算或批量改 pending |
| 飞书卡片停留在处理中 | 先核对产品快照，再看通知回执；模型完成和卡片发送成功是两个事实 |
| 追踪服务不可用 | 核对遥测告警与正式 Audit；不要把 trace 丢失解释为业务记录丢失 |
| 凭据泄漏 | 轮换泄漏凭据、限制相关日志/trace 访问，保留脱敏证据，按[数据请求流程](data-subject-requests.md)核对副本范围 |

详细恢复方法见 [Outbox](memory-outbox.md)、[通知](notifications.md)与[灾难恢复](disaster-recovery.md)。
