# Stage 10 运行与故障处理

## 进程与持久化

一个应用镜像启动三个角色：api 使用官方 AgentServer API 入口；worker 使用 `/storage/queue_entrypoint.sh`，其中包含原生 Core API 的启动；integrations 运行 `python -m financeclaw.integrations`。API 并发必须为 0，独立 Worker 并发必须为正。

`compose.yml` 在新项目卷中创建 `financeclaw_app` 和 `financeclaw_native` 两个数据库。只有业务空库执行 `alembic upgrade head`，原生库由 AgentServer 初始化。不要把旧开发库指向新初始迁移，不要用启动脚本自动删除已有库。

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 api worker integrations
```

对外提供产品 `/v1/*`，内部飞书入口仅接受服务凭据。普通产品 Token 直连原生资源也会被 Auth 拒绝；Ingress 应只放行所需产品路由。不要把代理 root_path 配成 SDK 内部免鉴权前缀。

## 单一业务模型

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

初始参数：command 并发 8、scanner 并发 8、join 容量 128、join 窗口 20 秒、租约 60 秒、15 秒续租、30 秒兜底扫描。对应 `FINANCECLAW_TURN_*` 设置。join 槽满不阻止命令受理；扫描继续公平核对到期 Turn。数据库断连后重试，过期 epoch 不能写产品状态。

API 停机取消本地观察并等待已开始的短事务完成，不向所有 native runs 发取消。Worker 重启由原生持久 runtime 恢复。若原生完成后业务提交失败，下一次核对会重新落账，最终答案和 outbox 仍唯一。

SSE 只有 `turn.snapshot` 与 heartbeat，ID 为 turn_id:revision。每进程共用一个 LISTEN 连接和批量快照刷新；重连返回最新快照，不补历史 revision。不得把 SSE 消费游标当执行游标。

## 保留与回收

历史索引/记忆删除随 integrations 消费现有 outbox。索引版本变化时显式重新入队，见[上下文运维](context-budget.md)。

checkpoint 回收保留在受控产品入口 `POST /v1/conversations/{id}/checkpoints/prune`。用户必须具有 `maintenance:checkpoints` 权限，Token 归属决定可访问会话；会话必须归档且业务与原生都无待办。默认预览，`apply=true` 才调用原生 prune。此操作使用同一个进程内 SDK，不给 integrations 的 Store 凭据增授 thread/run 权限。
