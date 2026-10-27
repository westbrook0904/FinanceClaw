# 持久触发与受控执行调度

已交付基线：`ad0b3cacc873134acc80e26ba252b44ef954cde5` 的 [Actions 40361051811](https://github.com/westbrook0904/FinanceClaw/actions/runs/40361051811) 已通过 2,110 Java、53 脚本与 45 Compose 检查，包含下述真实 PostgreSQL 调度、配额与并发用例。该结果不覆盖此后的文档或代码改动，也不是生产容量验收。[非 RAG 集中实施](non-rag-completion-plan.md)保留实施顺序、此前失败及本地 Docker 阻塞。

## 启用与管理

`FINANCECLAW_SCHEDULER_ENABLED=true` 启用 Cron 后台扫描，默认关闭；`FINANCECLAW_FAIR_DISPATCH_ENABLED=true` 启用公平/配额/worker 路由领取，默认保持既有 FIFO 领取。规则及 owner 配额管理需要服务端配置的 `schedules:manage` 权限；租户总量与租户内单 Agent 总量上限需要 `runtime:manage`。owner 和 tenant 只能从认证上下文产生。Webhook 事件使用同一认证入口和 `Idempotency-Key`，没有匿名外部触发入口。

创建/更新使用 `PUT /v1/schedules/{UUID}`，正文包含 `expected_revision` 和 `definition`。首次 revision 为 0，成功后递增；更新要求当前 revision 完全匹配。definition 字段：

| 字段 | 值与语义 |
| --- | --- |
| enabled | 是否允许新的触发；不取消已受理 Turn |
| kind | cron 或 webhook |
| expression / zone | cron 使用 Spring 六字段表达式与 IANA 时区；webhook 两项均为 null |
| misfire | skip：超出宽限时记录跳过窗口；fire_once：合并错过的时段执行一次；catch_up：逐个 occurrence 补受理，每轮最多 20 次 |
| overlap | allow：可产生不同会话的并行 Turn；skip：同规则有非终态 Turn 时保存跳过回执 |
| agent_id | 当前主体可调用的已发布 Agent |
| version_policy | fixed：固定保存规则时的发布；current：每次触发受理时解析目标当前发布 |
| message | 固定业务输入，1–32000 字符；事件调用不接受临时正文替换 |
| priority | -100 至 100，写入该 Turn 的派发元数据 |

`GET /v1/schedules` 和 `GET /v1/schedules/{id}` 读取本 owner 规则；`GET /v1/schedules/{id}/history` 读取持久触发回执，分页同时传最后一项的 `after=scheduled_at` 和 `after_key=event_key`，避免相同时间戳遗漏。`POST /v1/schedules/{id}/events` 接受空 JSON 和幂等键，仅适用于 webhook。

规则修订不可变保存。相同 webhook 幂等键始终回放原回执，包括规则后来被禁用或修订时。Cron 锁定到期规则并按原 scheduledAt 构造幂等身份；多个扫描者跳过被锁规则。触发受理、会话与 Turn、Command 和触发回执共同提交。无权限、暂停或发布不可用导致事务回滚；失败规则的同一修订延后 30 秒重试，不占住其他规则。最后错误仅保存固定 `admission_failed`，不保存异常或敏感输入。

skip/fire_once 会把未逐项枚举的错过窗口上限保存在 `coalesced_until`；不将这些时刻伪装成逐个成功执行。宽限默认 60 秒，可用 `financeclaw.scheduler.misfire-grace-seconds` 设置。DST 跳过/重复时刻由明确时区下的 CronExpression 计算，受理版本一经固定不再读取 current 替换。

## 公平、配额与路由

启用新领取策略后，短事务内串行化领取计数，先按 owner 上次成功领取顺序选择，再在该 owner 内按优先级加等待时长选择；每等待 30 秒增加一个排序单位，限制持续高优先级入队造成的饥饿。事务结束后立即释放调度锁，模型和工具不持有该锁。

- owner 默认最多 4 项 running，该 owner 的每 Agent 默认最多 2 项；`GET/PUT /v1/dispatch/policy` 以 revision CAS 管理。
- 租户默认总计最多 64 项 running；`GET/PUT /v1/dispatch/tenant-policy` 管理认证主体所属租户。
- 租户内单 Agent 默认最多 1024 项 running；`GET/PUT /v1/dispatch/agents/{id}/policy` 管理跨 subject 累计上限，包含该租户多个调用者共享调用同一 Agent 的情况。它与 owner、owner 内 Agent、租户总量上限同时生效。
- `PUT /v1/dispatch/turns/{turn}/priority` 仅允许修改本 owner 已排队的 Turn，沿用 Command → Turn 锁方向。running 不可重排。

降低上限不会撤销已领取工作，只阻止超限时再领取。running 数量从真实 Command 读取，过期但尚未恢复的 Command 仍占配额；不会通过忽略过期行放大实际外部执行。全局短事务调度锁繁忙时本轮返回无可领项，后续轮询兜底，不据此报告排空。

worker 必须已登记、未过期、active、协议版本为 1、具有 agentscope 能力；v3 发布还必须与 worker 的 runtime artifact hash 完全相符。原 Command 租约、Attempt fence、取消和恢复仍决定结果能否提交。worker 注册表只声明执行能力，不产生第二套任务状态。

## 验收入口

交付基线中的 `TriggerAndDispatchIT` 使用真实 PostgreSQL，覆盖并发重复 webhook、禁用后回放、固定/当前版本、重叠及错过回执、暂停受理的整体回滚、并发 Cron、owner/租户/Agent 配额、优先级老化和 worker 兼容性。`TriggerCadenceTest` 覆盖时区/DST 和非法规则；`LongChainWorkloadIT` 另覆盖四 owner 的持续负载。上述用例随文首完整 CI 通过，历史失败仍见[提交前验证记录](evidence/non-rag-implementation.json)。有限并发和离线模型夹具不建立生产吞吐、长期公平性或跨地域故障 SLA；实际外部服务与生产容量需在目标环境另行验收。
