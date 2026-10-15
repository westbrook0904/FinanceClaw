# M0–M2 框架能力复用审查

日期：2026-10-15。按项目固定的 AgentScope Java **2.0.3** 源码和运行测试审查，不以在线最新版文档替代当前依赖。JDK 21.0.12+8、Spring Boot 3.5.16、MyBatis Starter 3.0.5 保持不变。

## 已落实的调整

| 能力 | 采用的框架实现 | FinanceClaw 保留的边界 |
| --- | --- | --- |
| Agent 执行、工具循环与状态恢复 | `ReActAgent`、`AgentStateStore`、`RuntimeContext` | 每个 attempt 独立状态槽；租约/fence 与 CAS 同事务核对 |
| 执行事件 | `streamEvents`、原生事件类型、`FinalAnswerFilterMiddleware` | 只投影最终轮次正文及固定工具名/状态；持久 revision 与归属校验 |
| HTTP SSE | Spring MVC 的 `Flux<ServerSentEvent<...>>` 适配、Reactor 调度/取消/超时 | PostgreSQL 事件回放、连接数上限、只观察不触发执行 |
| 后台线程 | Spring `ThreadPoolTaskExecutor`、`@Scheduled` | 领取前执行槽限制、命令租约、定向中断与故障核对 |
| 权限与审批 | `PermissionContextState` / `PermissionRule`、`ToolBase`、`ConfirmResult` | 用户归属、action hash、问题版本/过期、授权 epoch 与当前 Turn 绑定 |
| 澄清 | 原生 suspended tool 与 `ToolResultBlock` 恢复 | 保存问题实例，按原问题 schema 校验后构造结果 |
| JSON Schema | SDK `ToolValidator` | 输入/审批分型与大小边界；没有自建 schema 验证器 |
| 模型与降级 | `OpenAIChatModel`、`GenerateOptions`、原生 `fallbackModel` | 每个模型发送前的完整请求预算、usage 校验、关闭 transport |
| MCP | `McpClientBuilder`、SDK Streamable HTTP 握手/list/call | 固定服务/工具/schema、只读白名单、重新授权和完整结果归档 |
| 数据库 | MyBatis Mapper、Spring 事务、Hikari、Flyway | 业务表、条件 SQL 与唯一约束；没有手写 JDBC 映射或额外 Repository 层 |

移除了 SSE 专用线程池、`Thread.sleep` 循环，以及业务执行入口对 `agent.call()` 的直接依赖。一个 Worker 只订阅一次 `streamEvents`，浏览器重连读取已保存的投影。不会把“多订阅一次就多执行一次”的 SDK Publisher 暴露为查询接口。

原生 `FinalAnswerFilterMiddleware` 要等本轮模型调用结束、确认没有工具调用后才放出正文。因此当前 SSE 是最终轮次文本块流，**不是每个 token 到达时立即转发**；工具进度不携带参数、原始结果、异常或 thinking。

## 为什么保留这些业务适配

**数据库状态适配。** 固定版的 `JdbcAgentStateStore` 已支持版本读取与 CAS，不能说框架没有 CAS。其实现自行获取连接并管理写事务；FinanceClaw 需要状态 CAS 与 Command fence 共用 Spring 事务，还需要删除墓碑及 tenant/subject/agent/release 命名空间。结合既定 MyBatis 选型，保留小型 `MyBatisAgentStateStore`，直接实现 SDK 接口，没有重新实现 Agent 状态机。[固定版 JDBC 源码](https://github.com/agentscope-ai/agentscope-java/blob/v2.0.3/agentscope-extensions/agentscope-extensions-jdbc/src/main/java/io/agentscope/extensions/jdbc/state/JdbcAgentStateStore.java)

**持久事件。** AgentProtocol 的默认事件总线使用进程内回放缓冲，完成后清理通道；它不提供本项目需要的数据库重连、重启恢复及跨副本游标。AG-UI MVC Controller 也绑定自己的运行入口与断连处理。直接引入整套 Controller 会形成两个任务入口，因此复用底层原生事件及 Spring SSE，保留 SQL 投影。[AgentProtocol 事件总线](https://github.com/agentscope-ai/agentscope-java/blob/v2.0.3/agentscope-extensions/agentscope-extensions-protocol/agentscope-extensions-agent-protocol/src/main/java/io/agentscope/extensions/agentprotocol/AgentProtocolEventBus.java)

**业务交互。** SDK 能挂起、接受确认并继续调用，但不会替应用校验 HTTP 身份、旧卡片、撤销授权、问题有效期及业务幂等。尤其原生确认后的执行可能绕过再次 ASK，动态授权必须在实际工具入口复查。

**请求预算。** 原生 context window、历史压缩和 tokenizer 辅助不等于最终供应商 HTTP 请求的预算。业务只在原生适配器完成序列化之后计算 UTF-8 保守上界，涵盖消息、工具定义、tool calls、结构化输出 schema 和额外参数；没有重写模型协议或 tokenizer。

## 本轮发现并修复的恢复问题

- `ToolResultBlock.of(...)` 默认是 `RUNNING`。澄清回复改为显式 `SUCCESS`，防止 SDK 已继续生成答案而业务仍判 waiting。
- 拒绝审批后，历史 ToolUseBlock 可仍为 `ASKING`，但已有 `DENIED` 结果。终态判定尊重拒绝结果，不重复发起审批。
- 恢复输入来自原快照，复制到新 attempt 的状态槽；不覆盖旧执行槽，也不接受客户端提交的 ToolUseBlock。

源码入口：[执行适配](../../src/main/java/com/financeclaw/agent/TaskAgentRuntime.java)、[SSE](../../src/main/java/com/financeclaw/api/TaskEvents.java)、[工具边界](../../src/main/java/com/financeclaw/agent/GovernedTools.java)、[治理事务](../../src/main/java/com/financeclaw/application/GovernanceService.java)。验证与尚未启用的范围见 [M2 使用与验证](m2-validation.md)。
