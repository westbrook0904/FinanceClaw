# M1：任务生命周期与本地验证

> **历史记录**：本文保留当时的设计、命令与验证结果，不代表当前独立应用的部署结构或最新测试结论。当前操作见[运行说明](../operations.md)，新库结构见[初始化说明](../architecture/fresh-schema.md)；历史类名、版本号和外部导出命令仅用于理解证据。

日期：2026-10-15。**本页为 M1 历史阶段记录；框架事件流、发布与授权见 [M2](m2-validation.md)，当前业务及最新回归见 [M3](m3-validation.md)。** M0 基线提交为 `05b2b488d23ce2312a6d94524c9d43208f75efdd`；M1 基于该提交继续实施，当前源码以[测试指纹](evidence/m1-results.json)为准。M0 的[原始报告](evidence/results.json)保持冻结，不代表 M1 新增源码的验证结果。

## 1. 实现范围

M1 建立了可运行的 HTTP → PostgreSQL Command → 后台 AgentScope → Journal / Outbox 闭环。使用同一套固定 JDK、Spring Boot、AgentScope 和 MyBatis 版本，没有引入 ORM、消息队列或通用 Repository 框架。

- 身份来自服务端配置的 Bearer token 对应的 tenant/subject；请求不能指定身份。未配置 token 时所有 `/v1/` 业务请求返回 401。此阶段是单个配置主体的开发认证，不是多用户登录系统。
- Conversation 归属检查、幂等键与正文摘要、单活动 Turn 由短事务和 PostgreSQL 唯一索引保证。重复提交仍返回初次受理的 `queued / revision=1`，并设置 `idempotent_replay=true`。
- Worker 用 `FOR UPDATE SKIP LOCKED` 领取 Command，记录不可变 attempt ID、递增 fence 和数据库时间租约；模型执行期间不持有业务事务或 M0 的 advisory lock。
- 每个 attempt 有独立 AgentScope 实例及状态槽，槽身份包含 tenant、subject、conversation、turn、command、attempt 和 release。状态保存同时检查租约、fence 和版本；旧 Worker 的保存、完成和失败写入均被拒绝。
- 完成事务同时写终态、最终 Journal、递增 revision 的事件和唯一 Outbox。M1 Integrations 只把 Outbox 投影到本地 `business.projections`，验证并发消费与幂等，不发送外部通知。
- SSE 读取持久业务事件，支持 `Last-Event-ID`。断连、重连与重复游标不会调用 Agent；不输出原始 SDK 事件、模型推理、工具参数或异常正文。

默认发布 `m1-offline-v1` 使用真实 AgentScope ReActAgent 和确定性离线 Model，回复 `已收到：<用户消息>`，没有外部工具。发布哈希包含该模型行为版本、SDK 版本和 `no-tools` 权限策略；未知发布拒绝运行。新 Turn 不复用失败/取消 Turn 的 SDK context，历史 Journal 可分页读取，历史上下文投影在 M2 完善。

## 2. API 与复现

先按 [README](../../README.md) 配置数据库、`FINANCECLAW_API_TOKEN` 并启动。示例依赖 curl 和 Python 3，仅处理本地开发数据；请在同一终端执行。

```bash
BASE=http://localhost:8080
CID=$(curl --fail -sS "$BASE/v1/conversations" \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["conversation_id"])')

TID=$(curl --fail -sS "$BASE/v1/conversations/$CID/turns" \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
  -d '{"message":"你好"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["turn_id"])')

curl --fail -N "$BASE/v1/conversations/$CID/turns/$TID/events" \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" -H 'Last-Event-ID: 0'
curl --fail "$BASE/v1/conversations/$CID/turns/$TID" \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN"
curl --fail "$BASE/v1/conversations/$CID/messages?after=0&limit=100" \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN"
```

预期受理返回 202，SSE 依次包含 `queued`、`running`、`completed`；最终状态包含 `result: 已收到：你好`，Journal 有用户与助手两条记录。SSE 终态发送完即关闭；未结束的流达到配置时限后关闭，客户端带最后 revision 重连。

| API | 行为 |
| --- | --- |
| `POST /v1/conversations`，正文 `{}` | 201，返回 conversation_id |
| `POST /v1/conversations/{cid}/turns` | 202；只接受 message 和 Idempotency-Key |
| `GET /v1/conversations/{cid}/turns/{tid}` | 业务快照；不暴露 command/attempt/SDK 身份 |
| `GET /v1/conversations/{cid}/turns/{tid}/events` | 持久事件 SSE；revision 为流游标 |
| `GET /v1/conversations/{cid}/messages` | after/limit 分页，只读 |
| `POST /v1/conversations/{cid}/turns/{tid}/cancel` | 202；需要独立幂等键，正文为空或 `{}` |

未认证返回 401；其他主体或会话错配返回 404；幂等正文冲突和活动任务冲突返回 409；未知正文属性、原生运行字段、非法游标或缺失幂等键返回 422。基础设施失败返回固定 503 错误码。

## 3. 完成、取消与恢复边界

完成分两个短事务：SDK 调用返回后，先锁住 Command，核对持久 AgentState 版本并保存同 attempt 的完成回执；再把回执转为业务终态和 Journal/Outbox。回执不会用新 attempt 补写，不会通过再次执行模型获取答案。

| 故障点 | 重启后处理 |
| --- | --- |
| API 已受理，尚未领取 | 新 Worker 正常领取原 Command |
| 已领取，尚无可证明的结果 | 租约过期后转 blocked，拒绝旧 Worker 写入 |
| SDK 已保存，但尚未保存完成回执 | blocked；快照本身不足以证明调用成功返回 |
| 完成回执已保存，尚未提交业务终态 | 核对原 attempt / fence / state_version 后补交一次 Journal/Outbox |
| 业务终态已提交 | 不重新执行；Outbox 可由多个实例幂等消费 |

`waiting` 依据未解决工具调用、ASKING、suspended 或 RUNNING 结果判定；工具失败/中断/拒绝不能仅靠后续文本冒充 completed。M1 仅验证挂起状态识别与持久化，用户审批/澄清回复入口及权限变更核对属于 M2。

取消和完成都先锁 Command 再锁 Turn。完成事务先提交则完成获胜；取消先写入 `cancelling`，该 attempt 停止后只能提交 `cancelled`。Worker 在心跳时读取取消意图并只中断对应 SDK 实例，同时取消其模型流订阅，避免无数据流一直占用执行槽。领取前或已停止的任务可直接取消；进程失联不能证明停止时先 blocked。终态不可反转，每个 Turn 只有一个终态 Outbox。

M1 的 blocked 可通过取消 API 结束，因为固定发布没有外部副作用；M2 注册写工具前必须加入工具调用账本、未知结果核对和独立 dispatch 防护，不能把这里的逻辑取消当作撤销外部操作。

## 4. 配置与资源边界

配置见 [application.yml](../../src/main/resources/application.yml)。开发默认一个进程同时运行 API、Worker 和本地投影消费者；可用 `FINANCECLAW_WORKER_ENABLED=false`、`FINANCECLAW_INTEGRATIONS_ENABLED=false` 分别关闭后台角色。多实例共享数据库，领取与投影不依赖进程内锁。

| 资源 | 默认限制 |
| --- | --- |
| Hikari | 8 连接，取连接 5 秒 |
| PostgreSQL | 连接 5 秒，socket 10 秒，SQL 5 秒，锁等待 3 秒 |
| Worker | 4 个并发执行槽；250ms 轮询；15 秒租约；2 秒心跳 |
| Model / SDK | 每次执行 30 秒上限；M1 无自动整条任务重试 |
| SSE | 最多 32 条连接，每条最多 30 秒，250ms 轮询 |
| Web | Tomcat 最多 32 个请求线程 |
| 退出 | 停止领取，定向中断，等待最多 5 秒；未确认结果由租约恢复器处理 |

`/actuator/health/readiness` 检查数据库、业务表及 AgentState 表是否可查询；Flyway 启动时校验并应用迁移，失败不启动。Artifact/S3 尚未启用，不列为本阶段必需依赖；启用后需增加对应超时及健康检查。公网认证、请求限流、反向代理 body/write 超时和四角色部署配置在后续交付阶段完善。

## 5. 可重复证据

2026-10-15 干净构建结果：**51 项测试通过，0 失败、0 错误、0 跳过**；其中 M0 回归 28 项、M1 集成 17 项、M1 状态判定 6 项。打包和 Spotless 格式检查通过。

独立 JVM 的实际退出结果另存于[退出证据](evidence/m1-process-exits.json)。

测试入口：[TaskLifecycleIT](../../src/test/java/com/financeclaw/m1/TaskLifecycleIT.java)、[ForkedTask](../../src/test/java/com/financeclaw/m1/ForkedTask.java)。所有并发与崩溃场景使用真实 PostgreSQL 16.10；崩溃注入使用独立 JVM 的 `Runtime.halt(73)`，恢复由另一个 JVM 执行。测试通过 SQL 将租约置为过去时间触发恢复，以避免等待测试时钟；恢复器仍使用真实数据库时钟与行锁。

| 验收编号 | 测试与实际核对 |
| --- | --- |
| T01–T02 | 8 个相同请求竞争只生成一个 Command；完成后重放返回原受理；变更正文拒绝 |
| T03 | 同会话竞争只有一个成功；不同会话的两个真实 SDK 调用同时进入模型 |
| T04 | 新 JVM 领取并完成已受理 Command |
| T05 | 领取后强制退出；新 JVM 标记 blocked；旧 attempt 保存/dispatch/业务写入均拒绝 |
| T06 | HTTP SSE 观察超时断开不执行任务；完成后带游标重连与重复游标返回一致事件 |
| T07 | 合成的待工具状态与文本写入真实状态库；业务保持 waiting，无最终 Journal/Outbox |
| T08 | 完成回执后强制退出；两次跨 JVM 恢复仍只有一条答案、一个 Outbox、一个 attempt |
| T09 | 8 轮取消/完成竞争，每个 Turn 仅一个终态事件和一个 Outbox |
| T10 | 终态后旧 attempt 的 seal/finish 拒绝；不能改写下一 Turn |
| T11 | 两套独立 MyBatis / 事务服务同时领取与消费，6 个任务各执行、投影一次 |
| T12 | 失败/取消后新 Turn 使用新状态槽；旧用户 Journal 保留可读 |

补充测试覆盖：未封口快照不重放、真实 SDK 中断、取消早于模型启动、完成事务异常、后台并发上限、心跳取消、模型超时、HTTP 非法字段和主体隔离。

```bash
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/collect-evidence.py
```

测试报告位于 `target/surefire-reports/` 和 `target/failsafe-reports/`，子进程退出证据位于 `target/m1/`；[归档结果](evidence/m1-results.json)列出全部案例、工具链和源码 SHA-256。仅声明本地验证，未运行远程 CI；真实模型、MCP、飞书、生产负载和历史数据迁移未包含在本次验收内。
