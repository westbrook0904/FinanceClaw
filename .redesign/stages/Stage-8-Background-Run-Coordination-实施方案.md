# Stage 8：后台自主推进、只读查询与可靠渠道通知实施方案

状态：Proposed（用户已确认总体方向并要求输出实施方案；尚未实现或通过发布验收）

编制日期：2026-09-07

适用基线：Stage 6 Fix A/B/C 持久化执行与交互，以及 Stage 7 紫微领域 Agent 候选实现。

本文是实施设计，不是已完成功能清单。参数为初始建议值，真实 Agent Server、PostgreSQL、
飞书及生产授权策略须按本文门禁验证。本文不批准 Stage 7 的规则、真实资料灰度或隐私例外。

## 1. 目标与现状

### 1.1 本阶段要解决的问题

将执行责任从“有人查询就顺便推进”改为“请求被持久化受理后，由后台持续负责推进”。
用户关闭页面、SSE 断开、飞书展示等待结束，不应成为主子任务停止衔接的原因。

目标承诺有明确前提：数据库和 Agent Server 可用、执行版本仍部署、授权有效且预算未耗尽。
满足前提时，已受理任务独立推进到完成、失败或等待用户；依赖故障与未知提交状态必须可见，
不能以后台重试为由承诺任何故障下都自动完成。

### 1.2 当前代码事实

| 位置 | 当前行为 | Stage 8 调整 |
|---|---|---|
| `application/feishu_channel_service.py` 的 `_resolve_final()` | 默认每 0.25 秒调用会话状态，展示等待上限 300 秒 | 展示与推进解耦；停止 250 ms 驱动式轮询 |
| `application/conversation_service.py` 的 `status()` | 默认允许派发 child 和恢复 parent，还调用交互恢复、执行对账 | 分离纯查询与内部推进，不能仅修改两个布尔默认值 |
| 同文件 `_observe_parent()`、`_advance_delegation()` | 接收 handoff、启动 child、交付子结果和恢复 parent | 保留业务语义，改由受租约保护的推进入口调用 |
| 同文件 `stream()` | 一次 Server Run 流结束后调用 `status()` 校正并可能推进 | 订阅不再承担派发、恢复或完成落库 |
| `interfaces/http/app.py` 的 lifespan | 启动时执行一次补偿，不存在持续的根任务协调 Worker | 装配持续 Worker；启动扫描只负责恢复可调度性 |
| `application/interaction_service.py` 的 `reconcile_owner()` | 无当前 scopes 时不领取尚未提交的交互恢复操作 | 增加有界后台授权依据，不能直接传 `None` 或 `*` |
| `modules/execution/tables.py`、`repository.py` | 固定执行快照、原子命令领取、未知提交对账、根预算与取消保护 | 继续作为事实与提交安全边界，不改成可超时重领的命令队列 |
| `modules/outbox/` | 已有 outbox 结构与单轮发布器，但不自动运行，也没有根结果到飞书的可靠投递闭环 | 复用事务发件箱模式；为通知定义独立投递记录与执行入口 |

另有两个实施前必须处理的事务边界：

1. `start_turn()` 当前依次调用 `begin_turn()`、`execution.register()` 和首次提交准备；
   各自持久化之间的崩溃窗口不能仅靠新增 Worker 消除。
2. `_record_completed()` 当前串联 Journal、Turn 状态及摘要构建；新增结果投影、事件和通知时，
   必须明确同事务边界，摘要失败不能阻止用户结果交付。

现有 `append_assistant_message()` 已将助手消息与 Turn 完成放在同一事务；Stage 8 应扩展这个
已有边界承载投影／待通知事实，不把已有原子性误说成缺失，也不在事务之外再补一个可能丢失的事件。

上述为当前工作树静态核对，不代表已定位某次线上日志或验证真实飞书负载。

### 1.3 与既有架构的关系

[最终架构设计](../00-最终架构设计.md)继续有效：LangGraph Agent Server 承担图执行、队列、
checkpoint 和 interrupt/resume；FinanceClaw 只处理业务归属、授权及跨父子 Run 的衔接。

[Stage 6 Fix](./stage-6-fix.md)和 [C 实施记录](./Stage-6-Fix-C-实施与验证.md)明确采用查询驱动，
并将离线自主推进、可靠通知排除在原范围外。本阶段显式扩展该范围，不把原选择描述为已承诺却未实现的功能。

新增 Worker 与 BFF 同代码库、同业务数据库，可以独立进程部署；它不是新建一个通用调度微服务，
不执行模型循环、不调度 Graph 节点、不保存第二套 checkpoint，也不生成任意 DAG。

## 2. 范围与分阶段交付

### 2.1 必须保留的不变量

1. 用户消息仍只进入根 `finance_agent`；模型通过受治理委派工具提出 handoff。
   协调器落实这个已存在的请求，不自行选择 Agent、编造参数或扩大任务。
2. 保留单活动根 Turn、单待交互位置、串行委派和根预算。一个 Turn 内仍可存在多次顺序委派。
3. 身份、原授权上界、实际发布版本、输入、request_clock、时区及工具绑定不得在恢复时漂移。
4. 子运行完成不等于根任务完成；交付失败、未知提交和待确认停止均不得误报 completed。
5. 查询、打开页面、接收 SSE、重复 webhook 和通知重试不产生新的业务委托。
6. 执行命令未知时先对账；绝不通过租约到期、换幂等键或重建 child 绕过不确定性。
7. 人工批准只覆盖原交互的具体动作；拒绝和取消后不允许用新委派绕过。
8. 渠道投递失败不改变任务结果，不触发模型重跑。内部 Audit 与用户通知独立。

### 2.2 交付阶段

| 阶段 | 实施范围 | 可验收承诺 |
|---|---|---|
| 8A：后台自主推进 | 原子受理、持久化协调、后台授权、状态投影、只读 GET/SSE、退避对账、迁移接管 | 不调用任何状态接口，任务也可完成主子闭环；进程重启可安全继续 |
| 8B：可靠结果与交互通知 | 持久化通知目标、待办投递、飞书回执、幂等与错误隔离、可恢复进度事件 | 展示连接断开后，已受理任务的最终结果及需要用户处理的交互仍有持久化投递责任 |
| 8C：唤醒加速与发布收敛 | 可选 Run webhook、查询与事件性能优化、压测、故障演练、旧驱动清退 | 事件优先、对账兜底；多实例与滚动发布通过正式环境门禁 |

8A 是本次修复的最小闭环；8B 完成前不能对用户承诺“结束后一定通过飞书主动通知”。
8C 中 webhook 是可选加速器，不是 8A/8B 的正确性前提；不兼容时继续使用后台退避对账。

### 2.3 不包含

- 多个领域 Agent 并行委派、递归委派、通用任务 DAG、定时命理分析或自动重新提问。
- 替换 LangGraph Runtime，或一期引入 Kafka、Celery、Temporal 等新的执行平台。
- 自动重启历史 failed 委托；用户要求重新尝试仍通过新 Turn 的模型决策发起。
- 根模型最终措辞与实际委派次数逐项校验；“重试了两个委托”的叙述准确性是独立问题。
- 扩大 Stage 7 五工具、计算规则、默认开关、模型权限或真实资料处理范围。
- 撤销已发生的外部副作用，或未经验证的远程 exactly-once 承诺。
- 完整渠道消息平台、任意通知订阅方、任意公网 webhook 目标和新审批 Web 页面。

## 3. 关键实施决议

以下决议为推荐实施基线，正式实现时记录 Accepted／调整及证据，不因文档存在自动冻结。

| 编号 | 推荐决议 | 主要代价与约束 |
|---|---|---|
| ADR-8-01 | 持久化根任务协调；Agent Server 继续拥有原生运行时 | 需要业务数据库迁移与 Worker 运维，不增加 Graph 调度抽象 |
| ADR-8-02 | 产品 GET 只读；POST 受理并持久化唤醒；Worker 负责后续提交 | 状态成为有版本的最终一致投影，需展示更新时间 |
| ADR-8-03 | 数据库到期领取与退避对账先落地，webhook 后置 | 最终状态发现存在可配置延迟，不能承诺零轮询 |
| ADR-8-04 | 协调租约可接管，执行命令领取不可因超时重领 | 未知提交可能保持待对账，牺牲部分自动恢复可用性以避免重复执行 |
| ADR-8-05 | 根任务具备有界、可撤销的执行授权；交互批准单独绑定 | 需要补充认证证据和重新授权入口，不能只复用过期 scopes |
| ADR-8-06 | 最终状态、Journal 引用和待通知事实原子提交 | 需给现有仓储增加明确的共享事务能力，不建设通用事务框架 |
| ADR-8-07 | 飞书优先可靠最终文本；流式卡片只作为可降级展示 | 未验证卡片重启恢复前，不能承诺恢复同一张卡片 |
| ADR-8-08 | 每个根任务持久化唯一驱动归属；混合发布需门控 | 旧二进制不能理解归属字段，不能直接与新 Worker 竞争同一根任务 |

## 4. 服务职责与对外契约

### 4.1 内部服务拆分

- `RunQueryService.get_status()`：校验租户／主体／对象归属，读取业务状态投影与已保存结果。
  不向 Agent Server 查询，不登记中断，不写终态，不提交任何 start/resume。
- `RunCoordinator.advance(root_run_id, lease_token)`：执行一次有界观察与衔接，返回后续调度建议。
  调用已有 Conversation／Delegation／Workflow／Interaction／Execution 服务中的内部命令能力。
- `RunDriveRepository`：持久化唤醒、到期领取、租约与防丢唤醒控制，只知道根任务工作责任。
- `RunAuthorizationService`：签发、收窄、撤销和校验有界业务执行授权，不负责身份登录。
- `RunNotificationService` 与发送 Worker：把已提交业务事件投递到固定渠道，不调用模型或委派服务。

内部 observe／advance 方法必须显式命名；不能保留“调用 status 后顺便修复”的隐式约定。
`advance()` 不能通过公开 HTTP GET 绕回应用服务，也不能递归循环到整个任务结束才释放 Worker。

### 4.2 写入与读取接口

| 入口 | Stage 8 语义 |
|---|---|
| `POST /v1/conversations/{id}/turns` | 保持 message-only；事务受理根 Turn、执行依据和唤醒，返回 202；不等待主子任务完成 |
| `POST /v1/interactions/{id}/responses` | 当前认证校验具体决定；原子保存决定、固定恢复操作和唤醒；202 不代表恢复已执行 |
| `POST /v1/runs/{id}/resume` | 保留兼容入口，路由至同一决定／恢复受理逻辑；不形成第二条自主提交链 |
| `POST /v1/runs/{id}/cancel` | 先持久化关闭后续派发及唤醒；Worker 确认整树停止后才投影 cancelled |
| `POST /v1/runs/{id}/reauthorize`（新增） | 同主体当前认证与幂等键下刷新有界执行授权；不接受目标、权限列表或任意 resume payload |
| `GET /v1/runs/{id}` | 只读状态、结果和安全交互投影；增加 `revision`、`updated_at`、`last_observed_at` |
| `GET /v1/interactions/{id}` | 只读已登记交互；过期可在响应中派生显示，持久化过期由后台处理 |
| `GET /v1/runs/{id}/events` | 只订阅，不创建／恢复 Run；连接断开不改变驱动归属或执行状态 |

状态沿用现有 `accepted`／`pending`／`running`／`waiting_child`／`interrupted`／`completed`／`failed`／
`cancellation_requested`／`cancelled` 的兼容口径；补充有限 `waiting_reason` 表达
`authorization_required`、`submission_uncertain`、`release_unavailable` 等原因。
驱动停用和基础设施故障与业务 failed 分开，展示“状态更新延迟”而不是猜测已完成或已失败。

终态回答来自 Journal 或受治理的结果引用。无变化查询不增加 revision；ETag/304 可选，
它只降低传输与解析成本，不负责推动任务。

### 4.3 必须覆盖所有查询分支

HTTP 当前按 RunService、WorkflowService、ConversationService、child 查询分支选路。
不能只改 Conversation 的状态方法，然后允许 Workflow 查询或 child 查询继续恢复执行。
所有产品可达的持久化根／子查询、交互查询和 stream-finalize 都纳入只读测试。

旧内部 smoke／兼容 RunService 不在本阶段改造为新持久化任务产品；若仍可被公共路由访问，
必须明确隔离或提供无派发副作用的兼容查询，不把它计入自主推进承诺。

## 5. 持久化模型与事务边界

### 5.1 `run_drives`：8A，根任务推进责任

每个持久化根任务至多一行；child 不另开竞争的根驱动。

| 字段组 | 建议字段与约束 |
|---|---|
| 归属 | `root_run_id` 主键，tenant、subject、conversation，受信任 root kind |
| 驱动模式 | `driver_mode=legacy/worker`，接管版本；对现存根显式迁移，不由查询者选择 |
| 调度 | `mode=ready/parked/stopped`、`next_check_at`、`park_reason`、`unchanged_count`、`last_error_code` |
| 租约 | `lease_owner`、`lease_until`、单调 `lease_epoch`；续租和提交必须匹配 token |
| 唤醒 | 单调 `wake_seq`、`handled_wake_seq`；唤醒与完成更新不能互相覆盖 |
| 授权与诊断 | 当前 `authorization_id`、`last_progress_at`、`last_checked_at`、created/updated |

为 `(driver_mode, mode, next_check_at)` 建索引，并支持过期租约的到期领取；外部观察必须绑定精确
`server_run_id` 和前驱。UTC 数据库时间用于租约／截止期判定，进程单调时钟只用于本地等待。

`ready/parked/stopped` 是检查责任状态，不是复制 Graph 节点状态；不保存模型 messages 或出生资料。
parked 若具有交互／授权等截止期，仍须被到期扫描唤醒；只有没有定时责任的 parked 才允许
`next_check_at=NULL`。stopped 不参加正常领取，不能依靠反复查询将它改回 ready。

### 5.2 `run_authorizations`：8A，后台授权依据

建议保存：`authorization_id`、root/tenant/subject、revision、原始授权来源与摘要、有限 scopes、
issued/expires/revoked 时间、策略版本、允许操作类别、原快照摘要及是否替代旧授权。
有效授权指针在根驱动中；变更生成新版本，不覆盖原执行快照或已准备操作的请求。

不保存用户 Bearer/JWT、刷新令牌、飞书 app secret 或任意凭据。
模型只能获得原 `ExecutionContext` 的必要字段；新增授权引用由可信装配层注入，不能成为工具参数。

### 5.3 `run_progress`：8A，可重建的产品投影

每个对外可见业务 Run 保存：归属、root/owner 关联、revision、公开 status、waiting_reason、
交互 ID/版本引用、最终 Journal／Artifact 引用、精确观察尝试、updated/last_observed 时间。

投影以执行日志、委派记录、交互和 Journal 为事实源，不保存第二份权威 Graph State。
根投影的 revision 在用户可见内容变化时递增；child 变化若影响根等待原因，也更新根投影。
旧记录没有投影时只能只读聚合已有事实，不能在 GET 中触发远程补偿或补写业务数据。

### 5.4 `run_progress_events` 与通知记录：8B

- `run_progress_events`：`(root_run_id, revision)` 唯一；保存事件种类、归属、安全摘要与引用。
  用于提交后事件补发和 SSE 业务进度回放，不记录 token、完整输入或完整命盘。
- `run_notification_targets`：持久化 app、channel、tenant、subject、chat、原消息、root 和投递模式；
  只能从验证后的渠道事件／已有绑定派生，不允许模型或公开请求体任意指定接收者。
- `run_notification_deliveries`：`(target_id, event_key)` 唯一；保存固定内容摘要／版本、可读取的
  安全内容引用、发送幂等键、分片索引、回执、尝试次数、下次重试、租约 token 与状态。

以上是命名建议，允许经评审合并存储，但不能丢失独立投递者、目标身份、稳定内容和回执语义。
普通 audit outbox 的单次 published 标记不能同时充当所有 SSE 订阅者和飞书发送器的消费游标。

### 5.5 必须落地的同事务边界

| 事务 | 必须一起提交的事实 |
|---|---|
| 根任务受理 | Turn＋用户 Journal＋不可变执行快照＋初始授权＋固定 start 操作＋drive＋初始投影；8B 开启后，来自飞书时还包含固定通知目标 |
| handoff 受理 | 唯一委派记录＋child 快照＋固定 child start 准备＋父 waiting 投影＋根唤醒 |
| 用户决定受理 | 交互决定与既有审批镜像＋固定 resume 操作＋本次授权依据＋Audit/outbox＋根唤醒 |
| 子结果交付观察 | 精确父恢复回执观察＋delegation delivered 事实及原 execution_status＋Audit＋下一投影／唤醒 |
| 业务状态提交 | 交互／等待／终态事实＋更新投影；8B 同时保存进度事件及对应通知待办 |
| 根完成 | 最终助手 Journal 幂等写入＋Turn 终态／释放单活动位置＋结果引用＋投影；8B 同时保存最终事件与投递意图 |
| 取消／撤销 | 关闭后续派发的持久化标记＋授权状态或取消意图＋Audit＋唤醒 |

通过现有 session factory 与仓储显式 `session` 参数完成必要组合；禁止远程 HTTP、LLM、
飞书发送、摘要生成跨入这些数据库事务。事务提交失败不能先给用户一个“已持久化受理”的 202。

首次执行所需 thread ID 预先确定并持久化；Worker 可以幂等确保 thread 存在，再提交固定 start。
必须验证实际 Agent Server 对同 thread ID 的创建／已存在行为，thread 创建回执不等于 Run 提交回执。
重复受理只返回原 Turn，不得用本次较高权限补齐旧缺失快照。

摘要构建移至完成事务之后的已有可重建补偿路径；摘要异常不回滚最终回答，不使模型重新运行。
纯扫描“存在 Turn 但缺快照”的旧数据只能报告问题，不能猜测其身份、输入或历史授权。

## 6. 协调 Worker 与执行算法

### 6.1 部署与领取

生产推荐新增同包入口 `python -m financeclaw.operations.run_worker`，与 BFF 使用相同的服务装配、
业务数据库与发布目录；Worker 不需要启动飞书长连接、HTTP 服务或一套额外模型循环。
开发允许在 lifespan 中托管相同 Worker，但持久化协议完全一致，不能依赖进程内任务集合保活。

PostgreSQL 用短事务 `FOR UPDATE SKIP LOCKED` 领取到期责任，写入租约并返回后释放锁；
该机制适合多消费者的队列式工作领取，不用于用户状态查询的一致性快照。
依据：[PostgreSQL SELECT 文档](https://www.postgresql.org/docs/16/sql-select.html)。

SQLite 用确定性单 Worker／CAS 测试路径；不以 SQLite 通过代替 PostgreSQL 多进程并发验收。
限制全局并发并避免单租户长期占满领取批次；租约续期与优雅停机必须显式实现。

### 6.2 一次 `advance()` 的顺序

1. 校验根驱动模式、租约 token、归属和当前精确执行位置。
2. 若有取消请求，进入停止确认分支，不派发任何新 start/resume；对账已在途尝试。
3. 对 claimed/uncertain 操作按固定 operation metadata 查回执；无法确认时只安排对账。
4. 观察当前根或活动 child 的精确 Server Run，不用共享 thread 的“最新 state”代替目标尝试。
5. 若有新命令待领取，复核发布绑定、原快照、后台授权、交互期限及预算，再调用现有 ExecutionService。
6. 按已观察事实受理 handoff、登记交互、校验子结果、准备父恢复或提交根最终结果。
7. 以租约／前驱／revision 条件提交投影和下一次调度建议；每次限制远程请求数和业务转移次数。

本阶段不以一条长事务覆盖整个链路，也不在一个 Worker 协程中长时间等待 LLM 输出。
观察一个远程状态所需的 get/join 请求数应计入实际探测指标，不能假设一次 advance 只有一次 HTTP。

### 6.3 各类状态的调度

| 观察类别 | 处理及下次检查 |
|---|---|
| 新受理／已授权 prepared 命令 | 尽快领取提交；提交后按精确回执观察 |
| pending／running | 仅检查活跃尝试，按 1 → 2 → 5 秒退避并加抖动 |
| 新合法 handoff | 同事务固定 child 请求，尽快安排下一步；不读取历史失败记录重新派发 |
| child 已完成／失败／拒绝但未交付 | 按唯一 `delivery:<delegation_id>` 准备或对账父恢复 |
| 原生 HITL／声明式交互 | 登记并投影 `interrupted`；停止执行状态的高频探测，安排交互截止期检查 |
| 用户回答已受理 | 唤醒原 root，恢复确切 owner 的原 interrupt；不是新 Turn |
| 领域 `needs_clarification` | 保持已有领域结果交付语义；根追问后本 Turn 结束，补充后新 Turn |
| 授权过期／撤销 | 停止新命令，展示 `authorization_required`；已提交尝试继续有限观察 |
| 交互过期且恢复未提交 | 保留已受理决定并展示过期原因，不自动延长批准期限 |
| claimed／uncertain | 只对账；超出对账告警阈值后降频并显示需要处理，不改 failed/未执行 |
| 发布缺失／不支持的中断 | 可见的需处理原因；禁止换 latest 或猜测恢复位置 |
| Agent Server／数据库暂不可用 | 有界重试、退避与告警；不转换成业务成功或悄悄新建 Run |
| 已确认根终态 | 停止正常推进；通知由独立发送器继续；重复事件不能复活该根任务 |

状态变化重置退避；根任务有执行在途时，授权过期也不能停止回执对账。
等待用户的根任务只保留期限、取消、撤销等检查责任；时间到期不等于远端任务已经停止。

### 6.4 防止租约接管与唤醒丢失

- 领取时保存 `lease_epoch` 与读取到的 `wake_seq`，续租／投影提交／调度完成都做 CAS。
- 外部唤醒原子递增 `wake_seq`，将到期时间提前，但不擅自删除其他 Worker 的有效租约。
- 完成本次检查时，若有更大 `wake_seq`，保持立即可检查，不能用旧的“无变化，5 秒后再查”覆盖。
- 旧 Worker 租约失效后禁止新准备／领取命令及覆盖状态。各提交仓储必须真正检查 token，
  不能仅在 `advance()` 开始检查一次，然后允许后续旧 Worker 无条件写入。
- 远程请求前已成功领取操作、但随后租约过期的情况，由稳定 operation 与回执对账保护；
  fence 不能撤回已经发出的远程 HTTP，不承诺瞬时终止所有在途请求。
- 同一时刻只能有一个合法根协调者，但仍保留操作级唯一性、精确前驱及 Journal 幂等约束，
  不把根租约当成唯一防重复机制。

### 6.5 明确区分三种“重试”

1. 观察重试：查询同一精确 Run，不触发新的模型／工具执行。
2. 提交恢复：prepared 可以被唯一领取；claimed/uncertain 只能查找原回执，不能定时退回 prepared。
3. 业务重试：用户新 Turn 经主 Agent 决策产生新的 handoff，使用新的业务身份并计入预算。

`run_drives` 租约可重新领取，`run_operations.claimed` 不因租约到期重新领取。
保持现有 ExecutionService 固定请求哈希与操作键的边界，不让后台“修复”重复触发历史失败委托。

## 7. 后台授权、交互与取消

### 7.1 授权签发与有效范围

当前 `AuthenticatedPrincipal` 只保留 tenant/subject/scopes；需从认证适配层传递经验证的
授权来源、到期时间及必要证据摘要，不接受客户端自报这些字段。

8A 推荐采用保守的有界授权：

- HTTP：后台授权到期不晚于已验证 JWT 的 `exp` 与配置的任务授权上限，两者取早。
  若未来希望超出登录令牌期限执行，必须另行批准明确的任务能力授权，不能自动延长旧 token。
- 飞书：由已验证事件身份、app、单聊绑定及允许列表签发有限期任务授权；执行时再检查当前
  允许列表、scopes 和配置版本。开发静态 token 同样必须有任务 TTL，不产生永久后台授权。
- 有效执行范围为原快照上界、有效任务授权以及当前可验证策略限制的交集；新增审批权限不进入
  普通执行上下文。服务身份只用于连接 Agent Server，不代表任意用户执行权限。
- 本地授权撤销与策略失效立即阻止新的命令领取。仅靠自包含 JWT 无法承诺外部 IdP 权限变更
  即时同步；没有已验证撤销／在线校验通路时，明确使用有限期语义，不能声称实时复验外部权限。

建议初始任务授权 TTL 上限为 30 分钟，属于待验收配置，不延长现有审批／交互窗口。
权限来源无法证明的旧根任务先停在 `authorization_required`；不得迁移出一个 `*` grant。

### 7.2 重新授权与已准备操作

新增 POST 重新授权入口仅限原主体，使用当前可信身份与幂等键生成新 grant，保留原上界。
它不替换输入、执行版本、request_clock、已接受决定或固定 operation payload，也不重新批准过期动作。
飞书可以增加显式 `/reauthorize <root_run_id>` 命令，继续验证原单聊归属；普通自然语言不默认为续权。
该入口只唤醒仍可继续的非终态任务；如果原执行已因权限拒绝等原因形成终态，重新授权不能复活它，
仍需按新的用户 Turn 处理。不得以新增 grant 作为任意失败运行重新执行的凭据。

已经 prepared 的请求可能绑定较宽 scopes，不能在恢复时静默改写请求导致同键不同哈希。
当前授权不足以覆盖固定命令时，保持等待重新授权或明确取消；本阶段不自动制造替代操作。
对于尚未形成操作的新 handoff，可以在原上界内固定收窄后的授权上下文。

交互回答先通过当前权限、版本、action hash、截止期与 owner 位置校验。
持久化的授权依据使 Worker 能继续提交“已合法接受、仍在有效窗口内”的决定，
不等于允许 Worker 自行回答或给任意 pending 交互批准。

### 7.3 在途执行的权限与预算

不能只在 Worker 提交 Run 时检查授权，否则一个长 Run 中后续 Tool 仍可能使用过期快照。
Stage 8 的新执行版本应在现有模型／工具治理与执行 Middleware 边界校验有效 grant、撤销、
取消和根预算；root/child 均使用可信 grant 引用，不能接受模型覆盖。

权限到期阻止后续受治理动作，但不声称能撤销刚刚发出的网络请求或已完成副作用。
旧已提交运行若不支持这类检查，必须保持旧版本的已知限制并排空，不能靠新增字段宣称即时生效。
授权检查失败仍允许系统只读对账和向原授权接收者展示安全状态，不再调用模型生成新的解释。

### 7.4 取消优先级

取消事务与命令领取使用同一根取消／预算保护边界：

- 取消先获胜，后续命令不可领取。
- 命令已领取或在途时，记录并确认停止，不将取消标记当成“从未执行”。
- 只有整棵已登记子树和未知提交均得到明确处理，才可投影 `cancelled` 并释放会话执行位置。
- BFF 重启、用户打开页面或再次收到相同 webhook 不得清除取消／拒绝标记。

## 8. 查询、SSE 与飞书展示

### 8.1 8A 的最低体验保证

GET 读本地投影；SSE 订阅当前已绑定尝试的只读流与根进度投影，parent 恢复到新 Server Run 后，
订阅方按绑定变化重新附着。根 Run ID 始终稳定，不能把一段 Server Run 流结束当根任务完成。

第一阶段允许订阅服务按较低频率读取本地 revision；不能每个浏览器分别对 Agent Server 高频查状态。
子 Agent 中间文字、工具原始结果、HITL 完整动作和 confidential 出生信息仍不直接投到根用户流。
流式 token 可最佳努力丢失；最终文本以已提交 Journal 为准，订阅无权自己补写 Journal。

飞书 `_resolve_final()` 不再调用推进方法。8A 可暂时使用 1～5 秒的只读投影退避等待，
等待超时仅结束展示并提示可查看结果；不取消执行、不返回虚假的失败，也不承诺尚未实现的后续通知。
单聊内存锁与全局信号量只覆盖短受理工作，不再等待整个 LLM 执行；数据库继续守住单活动 Turn。
这样等待期间用户可提交明确的审批／取消命令，不被长时间展示锁挡住。

### 8.2 8B 的业务事件订阅

提交后的 `run_progress_events` 支持根任务级事件与 `Last-Event-ID`；游标至少绑定 root 与 revision。
每个订阅者独立读取，不用“某消费者 published”代替所有客户端已经收到。
重连先发当前安全快照，再按协议补发可用业务事件；游标超过保留窗口时明确要求快照重置。

只对业务进度／交互／终态承诺可恢复事件，不建设全量 token 回放仓库。
事件默认建议保留 7 天，需在 8B 发布前确认；事件清理不能删除 Journal、执行证据或未投递通知。
SSE 心跳可为 15 秒的注释帧，不查询 Agent Server，也不增加业务 revision。

### 8.3 飞书通知目标与交互提示

任务受理时保存原单聊及原消息目标；交互响应沿用根目标，不因每次 `/approve` 消息创建第二套
最终答案订阅。命令受理回执可单独回复该命令消息，但不能形成两个最终发送者。

需可靠投递的事件至少包括：根 completed/failed/cancelled、需要用户处理的 pending 交互、
授权失效或明确需要处理的停顿。事件键使用稳定终态键或具体 interaction ID＋revision。
`cancellation_requested` 与 `cancelled` 不共用最终文案；普通轮询进度不逐条发送飞书消息。

发送前重新检查 app／chat／subject 绑定和渠道准入，防止目标撤销后仍推送敏感结果。
已经过期、已决定或已取消的交互通知应抑制或改为当前安全状态，不能晚到一条仍要求批准的过时提示。

### 8.4 投递回执与流式卡片边界

当前 `FeishuReplyGateway.send_text()` 返回布尔值，`stream_markdown()` 不持久化卡片身份。
8B 应增加最小发送回执类型，保存可用的消息 ID、稳定幂等键和错误类别；具体字段由 SDK 联调确认。
当前适配器会传 `uuid`，但不据此推断服务端无限期去重、跨方法去重或任意失败后的 exactly-once。

默认优先落实可恢复的最终文本通知；大文本按固定规则分片，每片有稳定内容摘要和发送键。
流式卡片可保留作实时预览，但最终通知必须由统一投递记录领取：

- 能持久化并恢复同一卡片时，保存 card/message ID，用固定内容进行幂等最终更新。
- 尚不能验证卡片恢复时，预览不冒充可靠最终交付；最终文本是单独明确的通知，不让实时展示器
  和 outbox Worker 同时发送完整最终答案。
- 8B 灰度前选择并固定每个目标的 delivery mode；不在回执不明时随意换方法、换键再次发送。

通知状态建议 `pending/sending/sent/uncertain/dead_letter/suppressed`。
响应丢失进入 uncertain；优先按原键／回执对账，仅在已验证的幂等窗口内安全重试。
超出已验证窗口或无法判定时展示投递异常并告警，不无限重发，也不重新运行 Agent。

现有 audit outbox 的领取实现不能直接当作通知多实例安全证明；通知领取必须带 owner/epoch，
防止旧发送器覆盖新租约。复用模式和必要代码，不把金融审计消费者改造成渠道消息消费者。

### 8.5 可靠性的起点

8A/8B 的执行承诺从“受理事务成功提交”开始。当前飞书 SDK 回调先排入内存任务，
回调返回与业务受理之间仍有进程故障窗口；不得把本阶段描述成保证所有收到的 SDK 回调都不丢。
8B 联调需明确 SDK 确认／重投语义；若产品要求覆盖受理前窗口，另行扩展持久化 inbound inbox，
不能在未验证平台重投行为时声称端到端不丢消息。

## 9. 唤醒来源与可选 webhook

### 9.1 必需的持久化唤醒

根任务受理、具体交互决定受理、重新授权、取消、执行回执绑定、子结果交付及内部状态变化，
均在业务事务中更新到期责任。周期扫描发现可恢复但缺失的责任时，只为可证明的已有执行事实修复索引。

不使用仅内存 `Event`、仅 Redis Pub/Sub 或仅 PostgreSQL NOTIFY 作为可靠事实；
这些机制即使后续引入，也只负责加速读取数据库。重启扫描分页处理，不能每秒全表扫描全部历史 Run。

### 9.2 Run webhook：8C 可选加速

Agent Server Run 接口支持指定完成回调；文档说明回调载荷还可能包含输入、配置和 state values。
出处：[LangChain 官方 Use webhooks](https://docs.langchain.com/langsmith/use-webhooks)。

新增受内部服务认证保护的回调入口，建议 `/internal/agent-server/run-events`；不对公众开放任意
执行／通知能力。回调只持久化唤醒意图，不直接提交 child/resume，也不直接采信回调中的终态内容。

处理规则：

1. 验证内部来源、认证头、负载大小和部署身份；使用配置的回调 URL allowlist，禁止模型提供 URL。
2. 通过本地 operation／Server Run 映射确认 thread、assistant、root 归属，不信任请求体自报 tenant。
3. 重复和乱序事件最多触发合并后的观察；旧尝试事件不能回退新投影或复活终态任务。
4. 回调可能早于 create/resume 回执落库：可以暂存有界、受认证的最小信号待绑定，或依赖到期扫描
   补偿；不能因为暂未映射就创建新业务执行。
5. 仅保存核验所需标识和安全摘要；HTTP access log、应用日志、审计、trace 均不保存完整载荷。
   不将真实出生资料发到公网 webhook 测试站，不将共享密钥放在 URL 查询参数。
6. 持久化成功后再确认回调；回调失败、遗漏或关闭时，后台退避对账仍必须完成同样闭环。

真实部署必须分别验证成功、异常、handoff interrupt、用户交互 interrupt、resume 后完成以及回调
重试／认证支持。锁文件版本或文档描述不能替代实际部署能力；不保证每个中间状态都有 webhook。
此处指 Agent Server Run 回调，不是 LangSmith trace 自动化 webhook。

## 10. 实施顺序、迁移与回滚

### 10.1 8A 的实施包

1. **A1 契约和行为抽取**：补现状回归；将 observe／advance 从查询路径抽出，明确所有 HTTP 分支。
2. **A2 持久化受理与授权**：迁移核心表；支持共享事务；补认证证据、grant 校验与重新授权入口。
3. **A3 Worker 与防重复**：有界推进、到期领取、fencing、防丢唤醒、取消／不确定操作对账。
4. **A4 查询与渠道解耦**：GET／SSE 只读投影；飞书短受理与只读展示；接管不依赖前台。
5. **A5 验证与灰度**：无 GET 场景、PostgreSQL 并发、宕机窗口、授权期限与混合部署验证。

8B 在 A 的同事务投影基础上增加业务事件、通知目标与发送器；8C 最后接入可选信号加速。
每包交付独立测试和说明，不能等最后一次联调才发现授权和事务协议尚未实现。

### 10.2 Alembic 与旧记录处理

当前迁移头为 `0008_stage6fix_c`。建议后续使用 `0009_stage8_coordination` 与
`0010_stage8_notifications`；真正开工前再次检查 head，禁止覆盖并行新增的迁移。

- 先扩展 schema，默认关闭新 Worker 提交；不在 Alembic 升级脚本中发网络请求或启动历史任务。
- 历史终态可批量只读重建投影，不唤醒、不补发未经明确授权的历史飞书通知。
- 活跃旧根只有快照、版本、前驱与授权都可证明时才接管；缺 grant 的旧根不因升级自动获得离线执行权限。
- 建立分页巡检报告：可接管、需重新授权、未知提交、发布缺失、缺快照／输入；问题分类不能都写成 failed。
- Stage 7 已有 chart/request_clock/时区与出生上下文快照不改写，不重新计算历史资料作为迁移补偿。

### 10.3 单驱动切换

1. 先部署理解 `driver_mode` 与命令保护的兼容版本到所有 BFF／渠道／恢复入口；此时仍由旧驱动负责。
2. 在隔离测试或只读 shadow 模式验证新协调器。shadow 不调用有副作用的旧 status，不领取执行操作，
   不写正式进度 revision／Journal／通知。
3. 确认 Worker 就绪、数据库迁移与授权策略完整后，按根任务 CAS 切换模式；新根从受理时固定模式。
4. 切换前处理旧路径在途提交；已 claimed 的操作由固定日志继续对账，不重新提交。
5. 对 worker 根，所有 GET／SSE／飞书路径即时只读；待 legacy 根排空后删除旧驱动开关。

无法理解模式的旧二进制不能与 Worker 同时处理相同根。若不能证明兼容滚动切换，安排受控停受理、
排空／记录在途操作、统一升级，再开启 Worker；不以“已有幂等”替代迁移所有权验证。

### 10.4 回滚

优先关闭新根接入、暂停新命令领取并保持只读查询／在途对账，不自动把 driver_mode 改回 legacy。
已迁移根必须由理解新授权与快照的兼容版本继续处理；禁止回滚到会忽略这些约束的旧二进制。
默认保留新增表和操作证据，完成排空与备份审查前不做 destructive downgrade。
回滚不清空租约以强迫命令重发，不解除用户拒绝／取消，不为恢复可用性换执行版本。

## 11. 模块与文件落点

以下是实施时的建议落点，本轮只新增设计文档与导航。

| 落点 | 内容 |
|---|---|
| `application/run_coordinator.py`（新增） | 有界根任务推进和结果分类衔接 |
| `application/run_query_service.py`（新增） | 所有持久化产品运行的纯状态／结果投影 |
| `application/run_authorization_service.py`（新增） | 有界任务授权及重新授权 |
| `modules/execution/` | 新驱动／授权／投影仓储；原操作日志增加必要的提交保护与事务参数，不改变未知重发语义 |
| `modules/conversation/repository.py` | Turn 受理与完成组合事务、幂等及序号分配 |
| `application/conversation_service.py` | 移除查询副作用，抽取受理／观察／完成能力 |
| `application/delegation_service.py`、`workflow_service.py` | child 与 Workflow 同样受根驱动、授权与精确尝试约束 |
| `application/interaction_service.py`、`modules/interactions/` | 决定＋授权依据＋唤醒同事务；查询不恢复 |
| `interfaces/http/auth.py`、`kernel/context.py` | 可信认证证据及必要的 grant 引用，兼容旧冻结快照 |
| `orchestration/agents/execution_middleware.py` 及既有治理入口 | 在途模型／工具动作的授权、取消及预算复验 |
| `interfaces/http/app.py`、`bootstrap.py` | 服务装配、只读路由、重新授权、Worker 健康与生命周期 |
| `operations/run_worker.py`（新增） | 独立 Worker 入口、优雅停机、巡检／接管诊断 |
| `application/feishu_channel_service.py`、`interfaces/channels/feishu.py` | 短受理、纯展示、授权命令、固定目标和发送回执 |
| `modules/notifications/`、通知应用服务与发送入口（8B 新增） | 业务事件、通知目标、投递记录与安全重试；不新建通用事件框架 |
| `application/ports/agent_server.py`、`infrastructure/clients/agent_server.py`（8C） | 可选回调参数与部署能力验证，不放宽公开 API Target |
| `infrastructure/migrations/versions/` | 增量迁移、索引、升级／回滚保护 |
| `infrastructure/settings.py`、`config/environments/`、部署文档 | 开关、轮询／租约／授权／通知配置及生产启动说明 |
| `tests/stage8/`（新增） | 单元、故障窗口、并发、查询只读、真实组件和渠道验收 |

不引入旧 `.design` 的 Runtime／Planner／Registry，不以工作线程池包装长期阻塞循环。
实施时保留当前工作树已有本地部署文档、Compose 与飞书生命周期测试的独立改动。

## 12. 配置、观测与运维

所有配置通过 `FINANCECLAW_` 前缀设置；字段命名在实现时与现有 Settings 对齐。

| 建议配置 | 初始建议 | 说明 |
|---|---|---|
| `RUN_COORDINATION_ENABLED` | 默认 false，灰度后开启 | 关闭表示不接入新 worker 根，不代表允许旧查询接管 |
| `RUN_WORKER_CONCURRENCY` | 8 | 根据连接池、Agent Server 配额和租户分布压测 |
| `RUN_DRIVE_BATCH_SIZE` | 不超过可用执行槽位的有界批次 | 避免领取后排队到租约失效 |
| `RUN_POLL_INITIAL_SECONDS`／`MAX_SECONDS` | 1／5 | 活跃尝试无变化退避；约 ±20% 抖动 |
| `RUN_DRIVE_LEASE_SECONDS` | 60 | 与远程超时和续租设计联动，不照搬为操作重发期限 |
| `RUN_DRIVE_RENEW_SECONDS` | 15 | 续租失败后停止新领取并交回检查责任 |
| `RUN_RECONCILE_INTERVAL_SECONDS` | 30 | 分页检查遗漏／异常责任，不扫描历史终态大表 |
| `RUN_AUTHORIZATION_TTL_SECONDS` | 1800 上限 | HTTP 不超过已验证 JWT 到期；不延长审批窗口 |
| `RUN_WEBHOOK_ENABLED` | false | 真实部署行为和安全验收通过后可开 |
| `RUN_NOTIFICATIONS_ENABLED` | 8B 前 false | 不影响执行驱动开关 |
| `RUN_NOTIFICATION_MAX_ATTEMPTS` | 8，明确失败适用 | 受幂等窗口／uncertain 约束，不能仅按次数盲重发 |

Worker 空闲等待需可被停机／唤醒中断；停机停止领取、等待有界在途调用，未确认的提交留给对账。
BFF 就绪检查应识别“新 worker 根已启用但没有兼容 Worker 心跳”的异常；已有持久化受理可保留，
但不能继续对外报告系统可正常推进。Worker 可用性和 Agent Server 可用性分别观测。

最小指标：到期任务积压／最老年龄、领取与过期接管次数、唤醒到开始推进延迟、状态无变化探测比、
精确 Agent Server 请求数、主子衔接延迟、uncertain 数量／年龄、授权等待、交互等待、
取消确认耗时、投递积压／失败／不确定回执。run/tenant 等高基数字段放受控日志，不用作无限指标标签。

INFO 记录业务状态变化、命令受理／回执、交互、终态与投递异常；无变化探测采用 DEBUG／采样。
分别检查 BFF HTTP access log、出站 httpx／SDK 和 Agent Server access log 的来源；不能仅降低
应用日志等级就声称 250 ms 负载已消失，也不能为降噪关闭安全 Audit 或错误日志。

初始性能目标是受控负载下唤醒到领取 P95 不超过 2 秒、无事件时单次终态发现通常不超过最大
轮询间隔加一次探测耗时；数据库／网络故障不在该延迟承诺内。最终 SLO 必须用真实部署压测确定。

## 13. 验收矩阵与发布门禁

### 13.1 自动化必测

| 编号 | 场景 | 必须断言 |
|---|---|---|
| S8-01 | 仅 POST 根请求，之后不调用 GET、不打开 SSE | root → child → root 完成或进入可见交互；外部查询调用次数为零 |
| S8-02 | 0、1、10 个观察者查看同一根 | 业务 start/resume 数相同；Agent Server 状态探测不随观察者数成倍增长 |
| S8-03 | 根受理每个持久化边界故障 | 202 前完整提交或整体回滚；幂等重放不补新权限、不重复 Journal |
| S8-04 | child 准备前后／提交响应前后进程退出 | 恢复原委派和固定操作；未知时不创建第二个 child |
| S8-05 | child 完成、父恢复准备或响应丢失后重启 | 原父恢复被精确对账；delivered 与执行终态不混淆 |
| S8-06 | 两个独立进程并发领取，旧 Worker 租约过期后返回 | 旧 token 不能覆盖投影／领取新操作；操作预算与 Journal 无重复 |
| S8-07 | Worker 检查结束与外部唤醒同时发生 | 新 wake_seq 不被延迟调度覆盖，不出现永久休眠 |
| S8-08 | 有效交互决定已提交事务，BFF 在远程提交前退出 | Worker 在有效授权／窗口内恢复精确 owner，不需再次 GET 或再次批准 |
| S8-09 | 无决定、已过期决定、错误 owner/hash/revision | 不自动批准、不换检查点；保持安全等待／冲突原因 |
| S8-10 | 授权到期、撤销、权限收窄、重新授权 | 不扩权；固定操作不改哈希；审批到期不因重新授权复活 |
| S8-11 | 长 Run 中后续模型／工具跨授权截止期 | 新执行边界拒绝后续受治理动作，已在途请求仍按事实对账 |
| S8-12 | 取消与命令领取并发、未知提交中取消 | 取消优先规则成立；未确认停止前不释放单活动根位置 |
| S8-13 | 主／子／Workflow／交互所有 GET 与 SSE-finalize | 除基础设施日志外不写执行事实；不调用 create/resume、不扣预算 |
| S8-14 | 父连续两次顺序委派，历史存在失败记录 | 仅处理当前精确 handoff；不重启历史失败；不错误限制为每 Turn 必定一个 child |
| S8-15 | SSE 断线、父恢复换 Server Run、飞书展示超时 | 后台持续；最终文本与 Journal 一致；child 中间消息不泄漏 |
| S8-16 | 最终结果提交各边界异常、摘要构建失败 | 无“completed 但答案／通知意图丢失”；摘要失败不重跑模型 |
| S8-17 | 飞书发送明确失败／响应丢失／sender 重启 | 仅重试原投递；稳定键与内容；uncertain 不盲换键，不增加模型／委派数 |
| S8-18 | 重复渠道事件、交互命令、通知回执及大文本分片 | 同目标事件／分片只有一条逻辑投递；最终输出不由两条路径重复发送 |
| S8-19 | 已过期交互通知排队、通知目标撤销或跨租户伪造 | 抑制过时提示、拒绝错误目标；不泄露问题／结果正文 |
| S8-20 | SSE 重连、事件游标过期和多个订阅者 | 独立业务事件回放／快照重置；不承诺 token 全量回放 |
| S8-21 | webhook 重复、乱序、回执绑定前到达、全部丢失 | 不直接执行；映射与权威观察成立；全部丢失仍能后台完成 |
| S8-22 | Agent Server／DB 暂不可用、Worker 全部停止再恢复 | 状态延迟可见；恢复后继续原操作，不重建执行 |
| S8-23 | legacy/worker 混合与二进制回滚 | 单根唯一驱动；未知提交不重发；旧版本不能绕过授权接管 |
| S8-24 | 完成／失败根收到迟到事件，再收到用户“重试” | 终态不复活；新用户 Turn 与旧任务严格区分 |
| S8-25 | Stage 7 合成出生资料及跨时区／跨日恢复 | 时间上下文不漂移；日志／回调／事件／通知遵守已有数据分级 |

### 13.2 分层验证

- 单元与应用集成：脚本化模型、Fake Agent Server、可控时钟和通知回执桩，验证状态与故障窗口。
- PostgreSQL：至少两个独立 Worker 进程，用真实事务验证领取、fencing、取消、预算及幂等；
  不能仅用同进程 asyncio Lock 或 SQLite 得出多实例安全结论。
- 真实 Agent Server：验证 thread 创建、精确 Run、interrupt/resume、回执丢失对账和服务重启；
  优先使用隔离发布与脚本化模型，线上供应商性能另行测量。
- 飞书：合成消息与测试单聊验证 SDK 回执、uuid 去重窗口、卡片模式、分片及进程退出；
  不以布尔 gateway 桩通过代替真实投递可靠性验收。
- 回归：现有 `tests/stage4`、`stage5`、`stage6`、`stage6fix`、`stage6fixc`、`stage7` 及架构测试；
  旧测试中“调用 status 推进”的断言需改为 Worker 推进，另保留 GET 只读的负向断言。

### 13.3 发布阻断

以下任何一项未解决都不能开启对应能力：

- 无观察者时不能闭环，或任何产品查询仍触发新执行。
- 跨进程重复派发、未知提交自动重发、旧租约覆盖新状态。
- 任务授权／审批／取消／发布版本存在绕过，或靠 `*`、latest 恢复。
- 202 前未完整持久化受理依据，或完成事实与最终用户结果／待通知事实发生永久分叉。
- 未知回执下无法安全处理却声称通知 exactly-once；飞书交互通知可丢失且没有持久化责任。
- 真实出生资料进入不符合原策略的日志、回调、模型或错误渠道目标。

8B 未通过不能宣称可靠主动通知；8C webhook 未通过可以保持关闭，但必须记录退避对账的性能边界。

## 14. 待确认项与本轮交付

不阻塞设计与隔离验证的推荐选择：

1. **后台执行期限**：先采用 30 分钟上限且 HTTP 不超过 JWT 到期；更长离线执行另行定义授权。
2. **飞书最终交付**：先保证可靠文本，流式卡片保留可降级预览；卡片原位恢复经 SDK 验证后开放。
3. **发布范围**：8A 先解决执行依赖查询，8B 紧接着补结果／交互通知；不等待 webhook 才切断前台依赖。
4. **旧任务接管**：可证明的任务明确迁移；缺授权先重新授权，缺快照／发布的任务保持可见停顿。
5. **受理前消息不丢**：本阶段不默认承诺 SDK 回调到数据库之前的窗口；若产品要求则单独扩展 inbox。

实现开始时优先确认授权期限、飞书交付模式及是否需要受理前 inbox。涉及超出原授权、真实渠道发送、
生产迁移或既有 Stage 7 隐私例外时，必须单独确认，不能把本次“输出方案”作为实施／发布授权。

本轮交付：本 Stage 8 实施方案与 `.redesign/README.md` 导航；未修改业务代码、数据库、依赖、
部署或远程仓库。后续实施另建 `Stage-8-实施与验证.md`，记录实际范围、命令、测试证据、未决项和发布状态。
