# 运行协调与调用观测

状态：本批开发已完成，提交前单元与契约验证通过。[本地记录与 Docker 阻塞](evidence/non-rag-implementation.json)独立保留；完整 PostgreSQL、独立进程和 Compose 结果以本提交对应 GitHub Actions 为准。不代表真实模型或观测后端验收。

## 权威与装配

`RuntimeCoordinationConfiguration` 由统一业务入口导入，装配事件门禁、worker 注册、本地运行工件目录、运行管理 API 和 `ObservationConfiguration`。受限离线入口不导入这组服务。

执行权威仍是 `Turn → Command → Attempt` 与数据库租约。注册表记录运行资源，不创建另一套任务状态。`ObservedAgentExecution` 是生产 `AgentExecutionPort` 包装器；原 `ExecuteTaskService` 继续负责取消确认、结果封存和最终提交。

## 跨实例事件

- `TaskEventHub` 继续承担本机提交通知和 AgentScope 临时进度；`text_delta`、工具进度没有持久游标，不在实例之间传播，也不能在断线后补回。
- `TaskEvents` 先建立本机订阅和持久唤醒订阅，再从既有持久事件表分页回放；每页最多 100 条。PostgreSQL `LISTEN/NOTIFY` 在事务提交后唤醒各实例，各订阅按自己的 revision 补读。空闲连接不轮询消息表。
- V14 为事件插入、会话 grant、共享资源授权、Agent 归档、隐私 owner/source 变化安装通知触发器。通知只含 Turn UUID 或全局 `*`，不含身份、正文或参数；通知不是授权事实，也不是事件日志。回滚事务不会通知，重复提示可以合并成一次最新 revision 补读。
- `Last-Event-ID` 只接受持久 revision。重复本机通知、补读和乱序通知按 revision 去重；发现缺口时先补读，再推进游标。终态、断开、超时或错误都释放订阅和连接配额。
- 本机队列与 Reactor 预取有界；每个持久唤醒订阅最多缓存一条待处理提示，监听线程不执行回放 SQL。最终交付处串行检查门禁，并且只在下游请求时执行检查，避免提前检查后把帧长时间缓存。每页补读和 keepalive 也经过门禁；keepalive 仅检查权限，不补读消息。
- `LiveTaskEventAccess` 在短事务中复核任务 owner、当前隐私来源/epoch/屏障、共享 Agent 当前访问权、冻结父计划中子 Agent 的结果来源授权和会话 grant revision。共享发布者身份不能替换任务 owner。其他实例撤权或建立隐私屏障后，通知立即唤醒读取门禁，后续交付被阻断；已交付给客户端的帧不能追回。
- 每个导入运行协调配置的业务 JVM 持有一条既有业务池连接，用短 Spring 事务提交 `LISTEN`，随后通过驱动阻塞收通知。接收超时只检查退出状态，不发送 SQL 心跳。该连接计入既有总预算，不借专用续租池、不新增第二个连接池。扣除续租预留后业务池至少为 2，否则启动失败；默认总预算 8 = 业务 6（含监听 1）+ 续租 2。
- 启动时监听尚未就绪或失联时，持久事件订阅返回 `503/persistent_events_unavailable`。失联立即终止旧流，包括还在初始回放或没有下游需求的流。重新提交 `LISTEN` 后发全局唤醒，再开放新订阅；客户端携带 `Last-Event-ID` 重连，初始回放补齐断线期间的提交。通知连接关闭在业务 DataSource 销毁前执行，终止物理连接并归还池槽，防止把仍处于 LISTEN 状态的会话放回业务池。

六参数 `TaskEvents` 构造器仅供明确的原单机 fixture；生产八参数装配必须提供门禁和通知订阅。这里保证各 standalone 实例间的持久事件恢复；临时增量仍要求与执行器处于同一 JVM，既有 split API 角色的 SSE 限制没有放宽。

## worker 注册、失联与恢复

`TaskWorker` 使用同一个进程 UUID 注册并调用 `claim(worker, lease)`。`business.runtime_workers` 保存协议版本、`agentscope` 能力、运行类摘要、并行度、活跃数、状态以及数据库时间的过期点。运行中的 command 数和实际执行槽位仍由既有命令状态、数据库租约和 worker 信号量决定。

- 当前 worker 声明协议 1；表允许正整数协议版本，调度器只接收它支持的版本。
- 注册续期先锁定真实记录，再以数据库时间判断仍存活；过期或 stopped 注册不能续活，同 UUID 不能通过重复注册复活。
- 独立控制检查刷新注册，并对活跃 attempt 执行现有 `ExecutionPolicyService.checkDispatch`，检查取消、共享撤权和隐私变更。失败后中断对应运行时并停止后续续租。
- 失去注册的进程停止新领取与续租，中断活跃执行，不在原 UUID 上重新登记。恢复需要新进程身份。
- 专用 command 心跳仍直接使用独立连接/事务资源，不先借业务池做注册或权限查询。worker 另以最后一次成功注册/刷新调用的开始时间加 TTL 维护保守的单调时钟截止点；即使普通控制调度被慢任务占满，领取与专用续租也会在本机截止后停止并中断执行。晚到的控制成功不能复活已经失效的进程身份；已发出的 SQL 仍由数据库锁/超时/租约决定结果。
- 注册失联不会直接写任务终态。command 租约过期后由既有恢复器读取封存结果并完成收尾；没有可确认回执的执行进入 `blocked/unconfirmed_execution`，不会重新调用模型猜测结果。

调度器在同一个 claim 事务内按协议、能力和发布中冻结的运行摘要选择兼容 worker，提交领取前再复核注册。兼容资源与任务调度的细节见 [触发与调度](trigger-scheduling.md)。

## 本地历史工件目录

操作员可配置受控目录，通过 `runtime:manage` 登记目录中的 JAR：

1. 操作员根目录先固定为真实路径，允许 `/tmp` 等系统别名；客户端只可指定规范相对 `.jar` 路径，拒绝绝对路径、穿越和子符号链接，目录被替换也会拒绝。不提供下载或任意代码执行入口。
2. 核对显式 SHA-256、文件大小与生产类摘要；校验结束再次核对字节摘要，拒绝校验期间发生变化的文件。
3. JAR 必须包含运行摘要类锚点。生产类按稳定顺序计算摘要；单类解压、类数量和累计类字节都有上限，避免压缩归档导致无界内存读取。
4. `business.runtime_artifacts` 按文件摘要登记路径、生产类摘要、字节数、协议和能力。重复登记必须完全相同；数据库触发器拒绝 UPDATE/DELETE，即使应用角色具有宽泛业务表授权也不能静默改目录。
5. `verify` 每次重新读取本地文件，不把历史验证当作文件仍然存在且未改变的证明。

文件摘要标识具体归档；运行类摘要用于执行计划与 worker 的兼容性匹配。登记工件不会发布版本、创建 worker、加载 ClassLoader 或执行该 JAR。历史运行资源仍由操作员显式部署，随后以注册事实参与调度。

## 调用链与性能观测

`RuntimeObservations` 使用本地 `ObservationRegistry` 记录 `financeclaw.runtime.call`：

| kind | 边界 |
| --- | --- |
| `attempt` | `AgentExecutionPort.execute` 的根运行调用 |
| `model` | 真实模型端口订阅，包括父模型、上下文摘要及业务子流程的供应商调用 |
| `tool` | 受治理工具动作订阅，包括 MCP 与已接入的只读子 Agent 工具 |

父子关系通过 attempt 在途映射和 Reactor Context 中的父句柄明确关联，不依赖 ThreadLocal 穿过线程切换。子调用优先继承同一根内的工具父句柄；无法继承时回到仍活跃的根。根结束时关闭残留观测并移除关联，晚到的订阅不能复活旧根。固定 workflow Driver 的本地步骤编排不冒充供应商模型调用。

低基数标签仅有 `kind` 和 `outcome`；outcome 是 `returned/blocked/fenced/error/cancelled`。不把 owner、任务/调用 ID、提示词、工具参数、SQL、模型正文或原始异常放进标签及 Observation error。观测 handler 的失败不重试业务调用、不替换业务结果，也不能阻断已完成的外部动作。另保留 `financeclaw.runtime.invocation` 固定 outcome 计时器。

`returned` 表示 SDK/端口返回，并非业务提交成功；后续封存、取消确认和租约 fencing 仍由原业务服务决定。已接入 Observation 接口和本地 handler 验收源码；没有增加 OpenTelemetry exporter、凭据或远程出口。是否导出及导向何处由部署的 ObservationHandler/监控配置决定，不能据此声称真实 tracing 后端已经接通。

## 调用成本与管理 API

`GET /v1/runtime/usage` 聚合已提交预算记录中的模型/工具调用次数，以及活跃/过期 worker 数。配置调用单价后返回估算值；它不是供应商 token 账单，不包含未提交计数、精确 token 用量或货币换算。未配置单价时估算为空，响应保留明确的 `costBasis`。

所有 `/v1/runtime` 接口都要求服务端身份目录授予 `runtime:manage`；普通 owner 身份不足以访问全局运行资源。内置 `runtime_manager` 角色授予该权限。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /v1/runtime/workers?limit=50` | 有界读取 worker 注册及实时有效性 |
| `GET /v1/runtime/artifacts?limit=50` | 有界读取登记的本地工件 |
| `POST /v1/runtime/artifacts` | 登记 `relative_path/sha256/protocol_version/capabilities` |
| `POST /v1/runtime/artifacts/{hash}/verify` | 重验登记工件的当前字节和类摘要 |
| `GET /v1/runtime/usage` | 固定维度的预算调用与配置成本估算 |

## 新配置

| 配置 | 默认值与限制 |
| --- | --- |
| `financeclaw.sse.notification-receive-timeout-ms` | 1000；100–5000，仅为阻塞接收的退出检查时限 |
| `financeclaw.sse.notification-reconnect-ms` | 1000；100–30000，监听失联后的重连退避 |
| `financeclaw.runtime.control-poll-ms` | 1000；100–30000 |
| `financeclaw.runtime.worker-ttl-ms` | 15000；1000–300000，至少为控制检查周期的 3 倍 |
| `financeclaw.runtime.artifact-directory` | 空，工件登记/核验默认关闭 |
| `financeclaw.runtime.model-call-cost` | 0；非负且不超过 1000000 |
| `financeclaw.runtime.tool-call-cost` | 0；非负且不超过 1000000 |

SSE 连接数量、超时、心跳和队列容量继续沿用既有配置。调度开关、身份目录和资源共享配置见各自架构文档。

## 验收入口

- `TaskEventsReplicationTest`：通知补读、去重、空闲撤权、慢客户端无需求时的临时帧门禁、初始回放中断线、离线配额释放和提示合并。
- `TaskEventNotificationIT`：两套真实 PostgreSQL LISTEN 连接与真实 MyBatis 回放，空闲 keepalive 不查事件表、提交/回滚通知、撤权唤醒、断线间隙恢复、关闭释放业务池连接。
- `RuntimeCoordinationIT`：两个独立 Spring 上下文共享真实 PostgreSQL；跨实例回放、grant/共享 Agent/隐私门禁、注册过期锁等待、原恢复链路、工件目录与成本读回；真实 AgentScope 模型与工具边界观测。
- `TaskWorkerRuntimeControlTest`：注册 UUID 与 claim 一致，撤权/注册丢失后中断、停止续租，同时保留恢复；假单调时钟验证没有控制 tick 时仍按保守 TTL 停止领取/续租，晚到成功不能复活。
- `RuntimeArchiveSafetyTest`：稳定类摘要、锚点与压缩归档上限。
- `RuntimeObservationsTest`：跨 Reactor 线程父子关系、失败/取消清理、观测 handler 失败不改变真实调用。

测试入口与最终结果分开记录；对应提交的完整 CI 报告给出数据库和部署结论，提交前记录不预测远程结果。
