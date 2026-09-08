# Stage 8 实施与验证

日期：2026-09-08。状态：**8.0 协议与技术验证完成；8A、8B、8C 尚未交付。**

本轮从 `804b722` 分包基线开始，采用 **PostgreSQL 持久化＋coordination 自行推进**。
按用户确认的方向，先具备基础能力，后续增强直接在 coordination 内实现，不引入 Temporal。
Coordinator Service、Webhook Ingress、Worker 的产品接线和数据库迁移仍属于 8A。
现有 BFF 的查询驱动路径尚未切换，不能将实验闭环称为产品已经具备后台运行能力。

## 1. 交付与代码边界

| 交付 | 路径与结果 |
|---|---|
| 协调契约 | `financeclaw/kernel/coordination.py`：Notification、Delegation／Interaction Request、TaskSubmission／ResponseDelivery、精确执行与 Continuation 引用、应用证据、静态能力门禁 |
| Backend Port | `financeclaw/coordination/backends/ports/backend.py`：六项最小能力；旧 AgentServerClient 保留给尚未迁移的业务入口 |
| LangGraph 映射 | `financeclaw/coordination/backends/langgraph_protocol.py`：包装既有 Handoff、绑定 run／checkpoint／interrupt、拒绝位置漂移、回调字段最小化 |
| 共享事务接口 | Conversation 的 `begin_turn`／`append_assistant_message`；Execution 的 `register`／`prepare`／`claim`／`bind`／`uncertain`／`request_cancel` 可接受同一 Session；`observe_in_session` 原子保存确切提交的终态证据 |
| 仓储回归修复 | 禁止相同 snapshot 将 child 改绑另一 root；SQLite 首次 SAVEPOINT 不得在外层事务回滚前提前提交 |
| 可复现实验 | `experiments/stage8/`；仅保留 PostgreSQL Worker，不进入 wheel，不接入生产 bootstrap，不新增正式调度表 |
| 决议 | [RD-032](../01-架构决议汇总.md#rd-032stage-8-采用-postgresql-worker) |

普通调用不传 Session 时保留原有短事务行为。显式 Session 的调用方负责 commit／rollback；
试验中的 HTTP 请求均在事务外。Journal 仍由原 Conversation 仓储写入，没有另一份 Task／消息真相。
实验投影复用业务 run_id；正式 Delegation 受理表、授权表和 Coordinator 进度表在 8A 建设。

## 2. 冻结的协议规则

1. `task_id` 复用 root／child 的业务 run_id，`operation_id` 固定一次出站命令；找不到回执
   只能是 uncertain，不能恢复为 prepared。重新领取协调责任不会创造新的提交权。
2. DelegationRequest 的输入直接使用原 HandoffRequest，按原参数口径生成 SHA-256。
   原 parent、child、目标版本、输入摘要与 result contract 必须匹配。重复观察不生成新请求身份。
3. ContinuationRef 固定原尝试、request、parent release、输入与原生绑定摘要。
   LangGraph binding 保存 thread／run／checkpoint／interrupt。恢复前再次核对原位置；
   缺失或已推进时拒绝，不能恢复 thread 的最新等待点。
4. Interaction 与 Delegation 使用不同响应类型；资料／选项按发布声明校验，审批绑定动作摘要。
   模型和回调都不提供执行身份、授权、backend URL 或 release 选择权。
5. `submitted` 只证明命令被接受。`ResponseApplicationEvidence` 要匹配恢复 operation、
   原 request／continuation、响应摘要及确切检查点。child completed 与 delivery applied 分开。
6. 一次性 backend 可通过 child 能力门禁；缺少可恢复请求、持久 continuation 或应用证据时，
   拒绝作为委派 parent。第二种真实 backend 尚未接入。

内联协议输入上限 16 KiB，Ingress 探针 body 上限 64 KiB。冻结模型的嵌套 JSON 并非深度不可变，
入库／出站需重新校验和比对摘要。更大的受治理 Artifact 引用、完整授权策略与外部身份接入属于 8A。

## 3. 共享数据库事务证据

实测 PostgreSQL **16.15**，使用独占 Docker 容器、随机数据库、真实 SQLAlchemy Session。
锁顺序固定为 Conversation → root execution → projection → coordination lease → 相关操作。
Worker 领取只锁自己的责任行并立即提交，不反向持有 root 锁；HTTP、模型和摘要不进入这些事务。

| 事务／竞争 | 实际断言 |
|---|---|
| 受理 | Journal、snapshot／初始 grant、start operation、projection、inbox／唤醒、event 共 6 个 flush 后分别崩溃，8 张事实／责任表均无残留 |
| 完成 | assistant Journal＋Turn 完成、确切 operation 终态、projection、event 共 4 个 flush 后分别崩溃，全部回滚；重放同一完成只保留一个助手结果 |
| 并发受理 | 4 个 OS 进程、独立连接、同一个幂等键，仅产生一个 Turn 和一条用户消息 |
| 并发领取 | 4 个 OS 进程竞争到期 root，只取得一个有效租约 |
| 失效保护 | 新 wake 不清有效 owner；旧 finish 保留新 wake；epoch 过期后无法写 projection 或结束新责任 |
| 版本 | 不支持的 driver_version 拒绝写入；恢复原兼容版本后继续使用原冻结 snapshot |

用户决定、Webhook 接收、Delegation 受理、交付确认、取消等组合事务的完整产品约束继续遵守
[实施方案 §4.2](./Stage-8-Background-Run-Coordination-实施方案.md#42-必须落地的组合事务)。
8.0 验证受理和完成的事务方法与组合原型；其他领域迁移、Audit/outbox 全量接线、授权 grant 表、
root active 唯一索引等是 8A 工作，不能用实验表代替其产品验收。

## 4. LangGraph 部署能力矩阵

验证环境是**真实本机 Agent Server dev/inmem 进程与 HTTP API**：LangGraph 1.2.11、
langgraph-api 0.13.3、SDK 0.4.4、runtime-inmem 0.33.3、checkpoint 4.2.0。
Graph 以原生 interrupt 和检查点运行，模型输入全部为合成数据。
这验证当前 API 版本的行为，生产的 Postgres／Redis backend 部署须在 8A 重新执行同一探针。

| 能力 | 结果及 8A 约束 |
|---|---|
| 成功／错误回调 | 收到 `success`／`error` |
| Delegation interrupt | 收到回调，status 为 **success**；精确 state 显示等待委派 |
| 用户 input interrupt | 收到回调，status 为 **success**；精确 state 显示等待用户 |
| child／parent resume 完成 | 两次新 run 均收到 success 回调；每次 resume 必须显式传 webhook |
| 静态鉴权头 | 环境变量模板有效；错误或缺失 token 被拒绝；URL 无 secret query |
| 出站字段白名单 | **0.13.3 实测启动失败**：字段先转换为 set，后置校验器却要求 list；报告保存实际启动验证结果 |
| 接收端最小化 | 只保留 backend 实例、opaque 执行标识、status hint、接收时间、摘要；kwargs／values／metadata 不入 Inbox |
| 503 重试 | 前两次失败第三次成功；持续失败实测 3 次后停止，需要后台补偿 |
| 线程 ensure | 同一预分配 thread 可重复创建；仅有 thread 不等于提交过 run |
| 精确尝试 | parent 恢复并完成后，观察旧 run 仍取得原中断检查点，不串用最新完成值 |
| 提交去重 | 不声明原生幂等；唯一业务领取＋operation metadata 精确查询恢复丢失回执 |
| 应用证据 | 原 parent resume 的 run metadata、checkpoint 与匹配 DelegationResult 的 ToolMessage 联合确认 |

因此 callback.status、回调携带 values、流结束都不能直接将任务标为完成。回调没有经验证的
稳定 event_id；探针按 deployment＋payload digest 去重，业务仍依 request／operation／revision
保持幂等。早到未关联通知保留最小事实，到期补偿确保当前绑定前后的责任不丢失。

8A 默认使用固定 callback URL＋静态鉴权头，Ingress 独立限制 body 并裁剪持久化字段；
不依赖 0.13.3 的 outbound allowlist。未来升级该配置必须先通过启动探针。loopback＋HTTP 例外
只存在于本实验动态配置，正式部署保留内部网络、TLS、来源绑定与密钥配置门禁。

官方说明与实测分别使用：[LangGraph Webhook](https://docs.langchain.com/langsmith/use-webhooks)
说明参数及静态 headers；白名单兼容性、中断 status 和本轮重试次数由实际版本探针确认。

## 5. Coordination 基础推进验证

使用 **2 个独立 OS Worker**、有界业务 Driver、真实 LangGraph 图和 PostgreSQL。
没有 BFF／GET／SSE 驱动。用户回答由测试程序显式受理，等待态只读查询不推进业务。

| 场景 | 实际断言 |
|---|---|
| root → delegation → child 用户交互 → child result → 原 parent resume → Journal 完成 | 完整闭环通过；4 个业务 operation 对应 4 个真实后端 Run |
| 重复／迟到回调 | 不增加 child、operation 或 Journal；旧回调不回退 revision |
| 所有回调丢失 | 持久化到期责任继续核对并完成 |
| 4 次出站远端成功、本地丢回执 | 按原 operation 找回；真实后端仍只有 4 个 Run |
| 两个 Worker 全部杀死后重启 | 使用原快照和持久化责任继续 |
| 等待 child 时取消 | 停止确切尝试，关闭后续派发 |
| 首次派发前授权过期 | 0 次远端提交 |
| 领取后无法查回执，再取消 | 保留 uncertain 与待对账，既不重发也不伪造取消成功 |

本轮已移除调度框架实验、依赖和切换分支。PostgreSQL 保存协调事实与责任，Worker 负责有限
推进；Backend Adapter 继续作为多 Agent backend 的扩展边界。

后续在 coordination 内按实际需要完善续租、退避、并发控制、诊断与监控。基础交付必须保留
幂等、操作回执、授权、取消与事务一致性；扩展调度策略不作为 8A 开始实施的前置条件。
报告 elapsed_seconds 包含启动、故障和测试等待，不是吞吐／延迟基准。本轮未做 HA、容量或
长期保留验证，实验也没有完整的长 HTTP 调用续租策略；8A 必须明确租约与 HTTP 超时配置，
继续在业务 operation 层防止重复派发。行锁机制参见
[PostgreSQL SELECT](https://www.postgresql.org/docs/16/sql-select.html)。

## 6. 验证与发布状态

- 工作区回归：`python -m pytest -q -m 'not external'`，**276 passed、7 skipped、2 deselected**。
  包含此前已有的 4 个未跟踪 Feishu lifecycle 测试；Stage-8 新增 22 个用例。
- Ruff check／format、secret scan、`git diff --check` 通过。
- [真实服务报告](../evidence/stage8-0/report.json)；
  [验证环境与源码摘要](../evidence/stage8-0/verification.json)。
- [复现说明](../../experiments/stage8/README.md)提供固定依赖、隔离服务与运行命令。

8A 下一步：正式表与 Alembic migration、BFF 原子受理、认证 Ingress、唯一 Worker／backend
适配、显式 Delegation 与用户决定、grant／取消／预算复验、结果原子落盘、只读查询和补偿。
8B 才建设业务事件投递／可靠飞书通知，8C 再执行生产部署、迁移、容量与故障门禁。
本轮保留了用户此前的本地部署文件和其他未提交修改，没有对远程仓库执行提交或推送。
