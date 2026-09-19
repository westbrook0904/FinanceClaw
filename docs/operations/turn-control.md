# Turn 运行、状态查询与故障处理

## 进程与持久化

当前部署使用一个镜像、四个角色。这里的 Turn 指一次用户输入及其所有澄清、审批、恢复和最终结果；它不等同于一个 HTTP 请求，也不等同于一次原生 run。首次启动按[本地步骤](local-full-stack.md)操作。

| 角色 | 入口与责任 |
|---|---|
| `api` | 官方 AgentServer API 入口；产品受理、命令提交、状态核对、SSE；`N_JOBS_PER_WORKER=0` |
| `worker` | `/storage/queue_entrypoint.sh`；原生图与 checkpoint 执行，包含 Core API；执行槽必须为正 |
| `integrations` | `python -m financeclaw.integrations`；飞书、通知、历史索引、记忆索引及索引删除 |
| `memory_worker` | 专用 `deploy/memory_worker_entrypoint.py`；SQL 来源许可下的记忆提取与整理，不执行原生图 |

`compose.yml` 在新项目卷中创建 `financeclaw_app` 和 `financeclaw_native` 两个数据库。只有业务空库执行 `alembic upgrade head`，原生库由 AgentServer 初始化。不要把旧开发库指向新初始迁移，不要用启动脚本自动删除已有库。

```bash
uv run --frozen python scripts/deploy.py
docker compose ps -a
docker compose logs --tail=100 migrate api worker integrations memory_worker
```

对外提供产品 `/v1/*`，内部飞书入口仅接受服务凭据。普通产品 Token 直连原生资源也会被 Auth 拒绝；Ingress 应只放行所需产品路由。不要把代理 root_path 配成 SDK 内部免鉴权前缀。

## 产品接口与响应

请求身份由产品 Bearer Token / OIDC 决定。先创建 Conversation，再在该会话创建 Turn；创建 Turn 只接收 `{"message":"用户原文"}`，客户端不选择 Agent、Workflow 或 native thread。

| 操作 | 产品接口 | 需要注意 |
|---|---|---|
| 创建会话 | `POST /v1/conversations`，正文 `{}` | 返回 201 与 `conversation_id` |
| 提交消息 | `POST /v1/conversations/{id}/turns` | 返回 202，受理成功不等于执行完成 |
| 查看任务 | `GET /v1/conversations/{id}/turns/{turn_id}` | 当前安全快照；`pending_interactions` 包含待答信息 |
| 观察变化 | 上一路径加 `/events` | SSE 的最新快照与心跳 |
| 回答问题/审批 | `POST /v1/interactions/{id}/responses` | 按交互类型提交正文与当前 revision，不另开 Turn |
| 停止任务 | Turn 路径加 `/cancel`，使用 POST | 先记录取消意图，再核对原生停止 |
| 授权/撤权 | Turn 路径加 `/authorization`，使用 POST / DELETE | 携带当前 `expected_grant_revision` |
| 查看原始问答 | `GET /v1/conversations/{id}/messages` | Journal 可用 `after` / `limit` 分页 |

提交消息、回答、取消和授权变更均要求 `Idempotency-Key`。网络超时后按原键重试同一操作；新内容使用新键。不得在尚未确定原任务状态时通过新建会话重复执行。

## 业务事实与状态

`conversation_turns` 固定输入与发布、保存当前 command、预算、有限授权、状态和观察租约。`turn_commands` 保存每次 start/resume 的不可变请求与真实 native_run_id。`interactions` 保存原生 interrupt、完整 checkpoint 和唯一用户决定。

命令遵循 prepared → sending → submitted → observed。sending 权利只能领取一次，不能因租约过期再次发送。网络结果未知时进入 uncertain：按 command metadata 穷尽分页查找原回执；没有结果不代表从未发送。不得改 command ID、补造 native run ID 或创建新 thread 绕过未知执行。

- accepted/queued/running：已受理、原生排队、原生运行。
- waiting：存在真实原生问题，使用当前 interaction revision 回答。
- resuming：决定与新 command 已在同一事务中落库，等待原生恢复。
- blocked：发布、授权、证据或提交结果需要处理；不能将它当作会话空闲。
- cancelling：仅确认取消意图，尚未证明原生停止。
- completed/failed/cancelled：已收尾，下一 Turn 可以受理。

取消与完成在同一 Turn 锁下竞争。取消先落库时，未知发送不能宣告 cancelled；完成先落库时，后续取消不会改写完成结果。重授权有明确期限，不能扩展原始权限上界，也不会重置模型/工具/命令预算。

## 观察、重启与参数

结果观察不依赖浏览器、SSE 或飞书连接。原生 `/join` 仅提示核对，最终必须校验当前 native run 和完整 checkpoint；原生 success 仍可能包含 interrupt。结果、Journal、审计、通知和索引意图共用一笔业务事务。

以下是代码默认值，均使用 `FINANCECLAW_` 前缀；生产值需结合负载验证。

| 变量 | 默认值 | 含义 |
|---|---:|---|
| `TURN_COMMAND_SLOTS` | 8 | 本进程命令提交并发 |
| `TURN_SCANNER_SLOTS` | 8 | 本进程状态核对并发 |
| `TURN_JOIN_SLOTS` | 128 | 原生 join 观察容量，不是图执行并发 |
| `TURN_JOIN_SECONDS` | 20 | 单次 join 观察窗口，秒 |
| `TURN_LEASE_SECONDS` / `TURN_RENEW_SECONDS` | 60 / 15 | 观察责任租约与续租间隔，秒 |
| `TURN_FALLBACK_SECONDS` | 30 | 兜底核对间隔，秒 |

join 槽满不阻止命令受理，扫描仍会核对到期 Turn。数据库断连后重试；旧租约 epoch 不能覆盖新责任人的产品状态。

API 停机取消本地观察并等待已开始的短事务完成，不向所有 native runs 发取消。Worker 重启由原生持久 runtime 恢复。若原生完成后业务提交失败，下一次核对会重新落账，最终答案和 outbox 仍唯一。

SSE 只有 `turn.snapshot` 与 heartbeat，ID 为 turn_id:revision。每进程共用一个 LISTEN 连接和批量快照刷新；重连返回最新快照，不补历史 revision。不得把 SSE 消费游标当执行游标。

## 按现象排查

| 现象 | 核对顺序 |
|---|---|
| 持续 `accepted` / `queued` | API 命令任务、原生 Worker 健康和队列；先区分未提交与已排队 |
| `waiting` 但没有卡片 | 先读 `pending_interactions`，再查[通知投递](notifications.md)；不要重跑模型来补卡片 |
| 恢复被拒绝 | 交互 revision、决定内容、归属、授权有效期及原发布版本 |
| `blocked` | 读取快照 `reason`，检查发布指纹、授权或原命令对账；不能当作空闲会话 |
| `cancelling` 长时间不变 | 核对原生运行是否真正结束以及发送是否未知；收到取消请求不等于已经停止 |
| 原生已结束但 Journal / 通知待处理 | 核对原生结果与业务提交是否完成，再查 outbox；连接结束不是业务提交证据 |

## 保留与回收

历史索引/记忆删除随 integrations 消费现有 outbox。索引版本变化时显式重新入队，见[上下文运维](context-budget.md)。

checkpoint 回收保留在受控产品入口 `POST /v1/conversations/{id}/checkpoints/prune`。用户必须具有 `maintenance:checkpoints` 权限，Token 归属决定可访问会话；会话必须归档且业务与原生都无待办。默认预览，`apply=true` 才调用原生 prune。此操作使用同一个进程内 SDK，不给 integrations 的 Store 凭据增授 thread/run 权限。
