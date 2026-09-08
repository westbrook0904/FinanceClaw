# Coordinator 运行（Stage 8A–8C）

BFF 始终通过 Coordinator Facade 受理用户命令并读取持久投影。Coordinator Service 由独立的
Webhook Ingress 与 Worker 组成。两者和 BFF 共用 FinanceClaw 应用库；
Agent Server 自己的 checkpoint/store、队列与图执行仍由 LangGraph 管理。
Coordination 使用 PostgreSQL Inbox、到期责任和租约推进，不需要 Temporal。

## 启动顺序

1. 为 BFF、Ingress、Worker 选择同一环境 profile，并叠加
   [`coordinator.env.example`](../../config/environments/coordinator.env.example)。
   三者使用相同 `FINANCECLAW_DATABASE_URL`、backend instance ID 和发布配置。
   Worker 使用既有 Agent Server 服务凭据与 Artifact Store；服务凭据不代表用户权限。
2. 使用本版本的 BFF／Agent Server 代码，再对共享应用库执行：

   ```bash
   .venv/bin/alembic upgrade head
   ```

   当前迁移头为 `0011_stage8c`，包含协调、通知、部署门闩和旧根证据表。
   部署环境关闭自动建表；迁移不接管旧任务或补发历史通知。
3. Agent Server 使用 [`langgraph.coordination.json`](../../langgraph.coordination.json)，
   注入 `LG_WEBHOOK_COORDINATOR_TOKEN`，值与 Ingress 的
   `FINANCECLAW_COORDINATOR_WEBHOOK_TOKEN` 一致，至少 32 字符。
   按实际内部 TLS 域名同时修改 URL allowlist 和 callback URL。
   `LG_WEBHOOK_` 是原生 API 默认允许的环境变量前缀。
4. 在各自进程启动入口（环境变量已注入）：

   ```bash
   .venv/bin/uvicorn financeclaw.coordination.ingress.app:create_default_ingress --factory --host 127.0.0.1 --port 8081
   .venv/bin/python -m financeclaw.coordination.worker
   .venv/bin/uvicorn financeclaw.bff.bootstrap:create_default_app --factory --host 127.0.0.1 --port 8000
   ```

   每条命令是独立进程。Worker 可以启动多个；默认每进程 4 个槽，按全局与租户上限短事务领取，
   HTTP 等待期间续租。SIGTERM 停止新领取并在配置的一步超时内结束。
   Docker 角色模板见 [`compose.coordination.yml`](../../compose.coordination.yml)，
   BFF 的现有部署也须叠加相同配置。模板不自动迁移数据库或启动 Agent Server。

`/health` 检查进程存活；Ingress `/ready` 区分数据库和兼容 Worker 心跳。
BFF `/ready` 也包含兼容 Coordinator 心跳与积压门禁。Worker 不提供 HTTP 端口。
本地明文回调只适用于 development/test；可复现验收脚本为 loopback 单独配置 allowlist。

## 运行语义

- `POST /v1/conversations/{id}/turns` 的 202 表示 Journal、Turn、执行快照、有限授权、
  固定 start 操作、Inbox、进度与 Audit／Outbox 均已提交；这时不要求 backend 已创建 Run。
- 主 Agent 可以委派一个 child Agent 或 Workflow；child 的提问、选择、审批由用户显式决定。
  child 结果交付原 parent，只有原恢复尝试的应用证据到齐后才标记 delivered。
  第一版支持顺序委派，暂不开放递归委派或多个并行请求。
- GET、SSE、飞书展示从应用库读取；无人订阅或 BFF 退出不会移除后台责任。
  SSE 提供当前快照与状态变化，保留 `assistant.completed`、`run.interrupted` 等已有事件格式；
  不承诺 token 回放。8B 支持 Last-Event-ID、独立游标及历史缺口的当前快照恢复。
- Webhook 只唤醒任务。原生 `success` 也可能是中断；Worker 精确查询该 Run 的 checkpoint。
  回调全部丢失时，后台按 `COORDINATOR_RECONCILE_SECONDS` 补偿。
- 操作一旦领取，即使回执丢失也只查原 operation metadata；查不到保留 `submission_uncertain`。
  不重置 prepared，不自动换键重发。无法证明远端不存在时需要运维处理，不能盲目“重试”。
- 取消先封闭整树派发，再逐一确认原尝试。未知提交仍未查清时保留 cancellation_requested，
  不释放当前 Conversation 的单活动根位置。已发生的外部操作不会回滚。

## 授权与交互

任务授权默认最多 30 分钟，HTTP 不超过验证后的 JWT `exp`；只保存来源摘要，不保存 JWT。
飞书依据已验证事件身份、单聊绑定和准入 scope 签发有限授权。开发静态身份也有期限。
运行中的受治理模型／工具动作持续检查 grant、取消和预算。

- 回答：`POST /v1/interactions/{id}/responses`，带 `Idempotency-Key` 与原 revision。
  input/choice 符合发布 Schema，approval 必须携带当前 action hash。
- 续期：`POST /v1/runs/{id}/authorization`。原主体用当次认证明确更新有限 grant；
  不改输入、发布、request_clock、命令 hash、已受理决定，也不延长交互的独立截止期。
- 本地撤销：`DELETE /v1/runs/{id}/authorization`。尚无 IdP 撤销事件自动同步。
- 飞书支持 `/authorize <root_run_id>`、`/revoke <root_run_id>`、`/cancel <root_run_id>`，
  必须属于原主体及当前单聊。写工具审批另需 `tools:approve`，Workflow 仍使用发布的审批 scope。

权限缩小后不足以覆盖原固定操作时保持可见等待，用户可取消或在原上界内重新授权。
授权、决定和取消状态变化与永久 Audit／Outbox 一同提交。

## 存储与运维边界

| 表 | 事实与保留 |
|---|---|
| coordinated_runs | 单活动根、当前投影、到期时间、wake 序号、owner/epoch/lease |
| run_authorizations | 有限 grant、来源摘要、期限、撤销与修订 |
| coordination_inbox | 最小通知／命令引用；短期保留 7 天 |
| backend_attempts | operation 到原生尝试的精确绑定与取消确认 |
| coordination_continuations | 原生等待位置及已应用原响应的证据 |
| run_progress_events | 安全状态修订事件，不复制回答或原始输出 |
| coordinator_heartbeats | backend/driver 对应的 Worker 存活证据 |
| coordination_control | 全库新受理／派发暂停、旧驱动封闭与停止证明摘要 |
| legacy_adoptions | 原始旧根事实、影子观察摘要与接管 CAS 依据 |

Delegation、Workflow、PendingInteraction、Journal、预算和审计继续复用既有表。
协调模式下 `run_executions.server_run_id`、`run_operations.server_run_id` 是
`backend_attempts.operation_id` 的索引键；原生 ID 仅保存在 adapter 的 reference/binding JSON。
兼容驱动拒绝这些根。排障时应通过 BackendAttempt 解析，不把索引键发给 LangGraph。

未知尝试通知每个 backend 最多保留 1024 条待关联记录；达到上限返回 503。
Worker 心跳每次最多补关联 100 条，过期未知通知清理，已关联任务仍有独立到期责任。
监控 `due_at` 延迟、过期 lease、`failures/last_error`、未关联 Inbox 数量和 Worker 心跳。
失败日志只记录错误类型，敏感内容不作为错误文本或事件正文传播。

已验证 API 0.13.3 的 `allowed_fields` 配置存在启动校验缺陷，因此示例没有启用该选项。
Ingress 先认证再限制 64 KiB body，丢弃原始 body，只保留受限标识、状态提示和摘要。
原生出站 webhook 仍可能含完整状态，只能发送到同一治理边界内的受信任内部 Ingress；
大 body 会被拒绝，后台精确观察负责补偿。更换 API 版本需要重新验证该行为。

## 迁移、回滚与后续阶段

`FINANCECLAW_COORDINATOR_ENABLED` 默认 false；开启后允许新根受理。关闭不恢复查询驱动，
已有根仍由独立 Worker 处理。
旧未完成根显示 `legacy_migration_required`，不自动迁移。内部直连 Tool／Workflow 入口
在协调模式拒绝创建无法后台管理的根；产品通过 Conversation Turn 表达调用与委派。

应先在隔离环境部署本版本的所有角色。存在活跃协调根时不能把旧 BFF 二进制混入部署，
也不能把关闭开关当作任务迁移。回滚先停止新受理并处理已有任务；只要七张新表中存在事实，
`alembic downgrade` 就拒绝删除，必须另做显式归档／迁移。

8B 已实现独立飞书通知责任与发送器，默认关闭新通知受理；启用和真实渠道门禁见
[通知运维说明](notifications.md)。未订阅的既有展示仍为尽力交付。
8C 已实现接管 CLI、部署门闩、容量限制和隔离演练；操作顺序和真实部署门禁见
[旧根接管与部署控制](coordinator-cutover.md)。
复现命令、真实服务版本与验收范围见 [Stage-8A 验证记录](../../.redesign/stages/Stage-8A-实施与验证.md)。
