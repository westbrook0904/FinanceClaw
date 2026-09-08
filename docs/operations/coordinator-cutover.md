# Coordinator 旧根接管与部署控制（Stage 8C）

本版本的生产 BFF 始终使用 Coordinator Facade。`FINANCECLAW_COORDINATOR_ENABLED=false`
只关闭新受理，不切回 GET／SSE 驱动，不停止已有根的后台责任。所有角色共享
`financeclaw_app`，迁移头为 `0011_stage8c`。无需 Temporal。

新根和接管根使用 driver 3。当前 Worker 能处理 1／2／3；封闭旧驱动时把已有 1／2
责任提升到 3，并撤销其旧租约。8A／8B Worker 不能领取 driver 3。
不理解部署门闩的旧 BFF 仍可能创建旧版本根，因此从旧版本切换采用**受控停止旧生产者**。
本仓库验证的滚动替换发生在理解同一协议的 driver 3 Worker 之间。

## 可接管范围

| 旧事实 | 处理 |
|---|---|
| 完整 start 尚未领取，远端查无原尝试 | 保留原 operation、输入、预算；接管后等原主体授权 |
| start 回执完整或能按原 operation 找回，backend 已完成或等待 | 绑定原 native run；不重发 start |
| parent 等 handoff，已有唯一 child；child 完成或等待用户 | 核对两处 checkpoint、原 child、参数与冻结资料；继续原父子关系 |
| 已展示但未回答的交互 | 保留 interaction ID、revision、问题、动作、截止时间及审批镜像 |
| 旧 resume／多次操作、已提交的旧决定 | 缺少原响应应用证明时保持 blocked，不自动转换或重发 |
| 原尝试仍在运行、未知回执查不到、缺输入／发布／身份／单活动位置 | 保持 blocked，先处理证据或等待原 backend 静止 |
| 独立旧 Workflow 无 Conversation 根 | 显示需要显式业务映射；不自动创建会话或根 |
| 历史终态 | 从原 Journal／Workflow 只读展示，不复活，不补发历史通知 |

原始行完整保存在 `legacy_adoptions.original`，受与执行账本相同的访问、备份和保留策略约束。
接管事务将旧命令转换为中立 `TaskSubmission`，把原生索引转换为 BackendAttempt 索引；
原命令 JSON／hash、native ID、原观察、状态、时间和预算均留在归档中。只改变协议表示和驱动，
不重新计算 Stage 7 资料引用、request_clock、timezone 或 release。原始证据摘要可复核。
接管不创建新的有效 grant；原主体必须显式重新授权，且不会延长交互期限。

## 操作顺序

以下命令由受控运维终端执行，环境中已注入正确业务库、backend 和固定发布配置。
CLI 不提供远程运维写接口。`inventory`、`shadow`、`control`、`diagnostics` 都是只读命令；
只有 shadow 会访问 backend，且仅查原 operation/run/checkpoint，不调用旧 status。

1. 停止流量中的新受理，记录并排空旧提交调用；先升级共享 schema，保持旧根不接管。

   ```bash
   .venv/bin/alembic upgrade head
   .venv/bin/python -m financeclaw.coordination.operations control
   .venv/bin/python -m financeclaw.coordination.operations set-control --revision 0 --admission-paused --dispatch-paused
   ```

   revision 必须使用实际查询结果。数据库暂停阻止兼容进程领取新 start/resume；已经领取的
   远端调用可能仍在途，继续查回执和取消确认。旧二进制不认识这个开关，必须在部署侧停止。

2. 停止旧 BFF、旧渠道／交互入口、Workflow／恢复任务、不兼容 Coordinator，并阻止调度器
   或 restart policy 把它们拉起。确认 Agent runtime 理解有限 grant 和原 continuation。
   保存可审查的部署证据，例如 `stopped-evidence.json`：

   ```json
   {
     "deployment_id": "实际部署标识",
     "recorded_at": "实际 UTC 时间",
     "legacy_bff_stopped": true,
     "legacy_channels_stopped": true,
     "legacy_recovery_stopped": true,
     "incompatible_coordinators_stopped": true,
     "restart_prevented": true,
     "agent_runtime_compatible": true
   }
   ```

   这些布尔值是操作员依据实际进程／编排器检查做出的确认；CLI 不会伪装成自动验证。
   文件由部署证据存储保留，数据库只存其 SHA-256。

   ```bash
   .venv/bin/python -m financeclaw.coordination.operations set-control --revision 1 --admission-paused --dispatch-paused --stopped-evidence stopped-evidence.json
   ```

3. 在暂停新受理后分页完成一次完整盘点。后续页使用返回的 `next_cursor`；每页最多 500 根，
   每根每类证据最多检查 500 行，超限根转人工处理。仍有新根写入时的盘点不作为完整清单。

   ```bash
   .venv/bin/python -m financeclaw.coordination.operations inventory --limit 100
   .venv/bin/python -m financeclaw.coordination.operations shadow RUN_ID
   ```

   修复 blocked 原因后重复 shadow。原 backend 仍运行时先等它进入可验证的终态／等待位置；
   查不到已领取操作不能重置为 prepared。旧 resume 需要额外的原响应应用证据和显式迁移实现。

4. 用审查得到的三个值提交接管。adopt 会重新执行只读 shadow，再按数据库原始指纹 CAS，
   影子观察与数据库事实改变都会拒绝提交。接管事务失败不留下部分归档或协调责任。

   ```bash
   .venv/bin/python -m financeclaw.coordination.operations adopt RUN_ID --fingerprint ORIGINAL_HASH --shadow-hash SHADOW_HASH --control-revision 2
   ```

   不批量跳过 blocked 根，不分配替代 operation ID。接管后的 `authorization_required`
   由原主体通过 `POST /v1/runs/{id}/authorization` 或飞书 `/authorize` 处理。

5. 启动兼容 Worker，检查诊断、积压和角色 readiness 后恢复派发与新受理。

   ```bash
   .venv/bin/python -m financeclaw.coordination.operations diagnostics
   .venv/bin/python -m financeclaw.coordination.operations set-control --revision 2 --no-admission-paused --no-dispatch-paused
   ```

   门闩封闭不可逆；恢复不会开放 legacy。不能启动忽略新协议的旧 BFF。
   旧服务代码仅保留作隔离验证和切换前兼容排空，生产默认装配不再暴露其查询推进路径。

## 容量、诊断和回滚

默认每个 Worker 4 个槽、每个 backend 最多同时领取 32 根、同租户最多 4 根。
PostgreSQL 用短 advisory lock 将容量检查和 SKIP LOCKED 领取组合，远程 I/O 不持锁。
所有 Worker 必须使用相同的全局／租户上限；进程数与每进程槽数共同决定实际可用容量。
SQLite 用于开发功能回归，跨进程容量证明以 PostgreSQL 为准。

`diagnostics` 报告兼容 Worker 槽心跳、到期积压及年龄、uncertain、未关联 Inbox、授权等待、
用户交互、child 结果待交付、取消未确认、通知状态、当前库锁等待和本进程连接池占用。
任务 ID 只出现在盘点／日志／trace，不作指标维度。backend 健康标为 `not_probed`，
真实 backend 和渠道探测由各自验收报告提供，数据库正常不能代替这些探测。
BFF 允许新受理或仍有活跃根时，缺少 driver 3 心跳会不就绪；最老到期积压超过配置
`COORDINATOR_READY_BACKLOG_SECONDS`（默认 120 秒）也会不就绪。

SIGTERM 停止新领取，当前各步按 `COORDINATOR_MAX_STEP_SECONDS` 有界收尾；容器优雅停止
时间应更长。数据库暂时失联时 Worker 停止领取并等待恢复；迟到回执仍需原 owner/epoch。

回滚先暂停 admission 和 dispatch，保留只读服务、兼容 Worker、原 Inbox 和回执核对。
通知发送器按其独立开关和回执恢复规则处置，不能回到旧的最终消息发送路径。
一旦存在 driver 3 根、接管归档或控制 revision 变化，8C downgrade 会拒绝删表。
不要用降版本、复制 active root 或清空 uncertain 绕过此限制。

正式部署门禁和可复现命令见 [8C 实施与验证](../../.redesign/stages/Stage-8C-实施与验证.md)。
