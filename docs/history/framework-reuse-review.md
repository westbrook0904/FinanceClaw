# M0–M5 框架能力复用审查

> **历史记录**：本文保留当时的设计、命令与验证结果，不代表当前独立应用的部署结构或最新测试结论。当前操作见[运行说明](../operations.md)，新库结构见[初始化说明](../architecture/fresh-schema.md)；历史类名、版本号和外部导出命令仅用于理解证据。

日期：2026-10-15。按项目固定的 AgentScope Java **2.0.3** 源码和运行测试审查，不以在线最新版文档替代当前依赖。JDK 21.0.12+8、Spring Boot 3.5.16、MyBatis Starter 3.0.5 保持不变。

进一步分析见 [AgentScope 复用与 COLA 架构整改方案](architecture/agentscope-cola-remediation.md)：包含重复核查、当前依赖环、目标包结构和 R0–R7 实施顺序。第二轮 [Spring 事务与组件复审](architecture/spring-transactions-component-review.md) 已明确统一采用 Spring 事务，完成 4 项隔离 JDBC 实验；随后已实现兼容样本、M0 隔离、JSON/摘要抽取、Operations 分层与 7 项 ArchUnit 门禁，代码与实际验证范围见 [R0–R2 实施记录](architecture/r0-r2-implementation.md)。

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
| MCP | `McpClientBuilder`、`Toolkit.registration().mcpClient(...).enableTools(...)`、原生 `McpTool` | 部署 host/tenant/凭据引用、只读工具白名单、scope/预算与 Artifact 归档 |
| Skill | `ClasspathSkillRepository` → `ReActAgent.Builder.skillRepository` → 原生渐进加载工具 | 发布资产指纹、`skill:use` 授权与根预算；没有自建 Skill 状态或加载器 |
| 数据库 | MyBatis Mapper、Spring 事务、Hikari、Flyway | 业务表、条件 SQL 与唯一约束；Operations 通过窄 gateway 隔离 Mapper，没有通用 CRUD 框架 |

移除了 SSE 专用线程池、`Thread.sleep` 循环，以及业务执行入口对 `agent.call()` 的直接依赖。一个 Worker 只订阅一次 `streamEvents`，浏览器重连读取已保存的投影。不会把“多订阅一次就多执行一次”的 SDK Publisher 暴露为查询接口。

原生 `FinalAnswerFilterMiddleware` 要等本轮模型调用结束、确认没有工具调用后才放出正文。因此当前 SSE 是最终轮次文本块流，**不是每个 token 到达时立即转发**；工具进度不携带参数、原始结果、异常或 thinking。

## 为什么保留这些业务适配

**数据库状态适配。** Spring 负责事务生命周期，MyBatis-Spring 负责会话参与；当前 `MyBatisAgentStateStore` 已采用该方式，只补充 SDK 状态接口和业务隔离规则。原生 `JdbcAgentStateStore` 已有版本读取与 CAS，但 2.0.3 在外部事务连接上仍主动 commit。第二轮实验确认单加事务感知 DataSource 会提前提交此前的业务 SQL，因此不作为接入方案。继续复用 Spring/MyBatis，让状态 CAS 与 Command fence 同事务，并保留墓碑和命名空间；无需引入 SDK 自己的 JDBC 提交路径，也不自建事务框架。[固定版 JDBC 源码](https://github.com/agentscope-ai/agentscope-java/blob/v2.0.3/agentscope-extensions/agentscope-extensions-jdbc/src/main/java/io/agentscope/extensions/jdbc/state/JdbcAgentStateStore.java)

**持久事件。** AgentProtocol 的默认事件总线使用进程内回放缓冲，完成后清理通道；它不提供本项目需要的数据库重连、重启恢复及跨副本游标。AG-UI MVC Controller 也绑定自己的运行入口与断连处理。直接引入整套 Controller 会形成两个任务入口，因此复用底层原生事件及 Spring SSE，保留 SQL 投影。[AgentProtocol 事件总线](https://github.com/agentscope-ai/agentscope-java/blob/v2.0.3/agentscope-extensions/agentscope-extensions-protocol/agentscope-extensions-agent-protocol/src/main/java/io/agentscope/extensions/agentprotocol/AgentProtocolEventBus.java)

**业务交互。** SDK 能挂起、接受确认并继续调用，但不会替应用校验 HTTP 身份、旧卡片、撤销授权、问题有效期及业务幂等。尤其原生确认后的执行可能绕过再次 ASK，动态授权必须在实际工具入口复查。

**请求预算。** 原生 context window、历史压缩和 tokenizer 辅助不等于最终供应商 HTTP 请求的预算。业务只在原生适配器完成序列化之后计算 UTF-8 保守上界，涵盖消息、工具定义、tool calls、结构化输出 schema 和额外参数；没有重写模型协议或 tokenizer。

## 本轮发现并修复的恢复问题

- `ToolResultBlock.of(...)` 默认是 `RUNNING`。澄清回复改为显式 `SUCCESS`，防止 SDK 已继续生成答案而业务仍判 waiting。
- 拒绝审批后，历史 ToolUseBlock 可仍为 `ASKING`，但已有 `DENIED` 结果。终态判定尊重拒绝结果，不重复发起审批。
- 恢复输入来自原快照，复制到新 attempt 的状态槽；不覆盖旧执行槽，也不接受客户端提交的 ToolUseBlock。

源码入口：[执行适配](../../src/main/java/com/financeclaw/infrastructure/agentscope/execution/TaskAgentRuntime.java)、[SSE](../../src/main/java/com/financeclaw/api/TaskEvents.java)、[工具边界](../../src/main/java/com/financeclaw/infrastructure/agentscope/execution/GovernedTools.java)、[治理用例与事务分层](architecture/governance-decoupling.md)。验证与尚未启用的范围见 [M2 使用与验证](m2-validation.md)。

## M3 增量审查

- MCP 由 AgentScope 建连、发现工具、转换 Schema、调用和转换结果；FinanceClaw 不再直接使用 MCP 底层客户端、手动 list/call 或维护 `input-structure/1` 比较器。服务端名称/版本不再做本地固定值比较，新 attempt 使用服务端实际公布的 Schema。只读工具白名单之外的工具不注册；酒店/Taibu 的业务适配只负责授权、预算、领域校验与 Artifact 归档。
- Skill 采用 2.0.3 推荐的 `skillRepository` 入口（不使用已弃用的 builder `skillBox`）。SDK 自己生成目录提示、注册 `load_skill_through_path` 并读取正文/资源；移除了 `SkillSession`、`SkillPackage`、`skill_refs` 读写逻辑、手写请求投影及独占加载规则。旧表保留停用，历史 Flyway 迁移保持原样。正文按原生工具结果进入 AgentState，恢复复用该状态。`skill:use` 通过原生 `MiddlewareBase.onActing` 复查并扣根预算，因为该版本 Skill 工具是 `AgentTool`，不经过 `ToolBase` 权限引擎。
- 子 Agent 使用原生 `ReActAgent.call`、权限、状态存储、取消信号。未直接使用通用 `SubAgentTool`：固定 2.0.3 实现允许调用方传入 session_id，独立保存时使用空 user；它支持流式转发和不转发内部事件的调用路径，事件转发并非无法关闭。当前主要保留服务端子任务身份、根预算、权限收缩和 fence 约束，由小型 `ToolBase` 入口绑定；原生替换仍需等价验证。[固定版 SubAgentTool](https://github.com/agentscope-ai/agentscope-java/blob/v2.0.3/agentscope-core/src/main/java/io/agentscope/core/tool/subagent/SubAgentTool.java)
- 固定 Workflow 用确定性 Model 驱动器产生约定的工具步骤，仍由原生循环执行、ASK 挂起及 ConfirmResult 恢复；驱动器不调用 LLM、不启用模型 fallback、不计为模型消耗。MyBatis 仅保存应用步骤和本地发布效果，未实现另一套通用图引擎。

调用路径和验证范围见 [M3 业务与验证](m3-validation.md)。

原生 MCP 2.0.3 的内容转换读取 `content`，不把 `structuredContent` 单独传入 ToolResult。因此酒店/Taibu 必须在文本 content 中提供 JSON 结果；只有 structuredContent 的服务目前会明确失败。没有为此实现第二套协议转换器。M3 的真实服务兼容性仍待联调。

## M4 的原生能力与保留边界

- 结构化提取使用 `ReActAgent.call(..., Extraction.class)` 和 `Msg.getStructuredData`，embedding 使用 `OpenAITextEmbedding`。固定版 `LongTermMemory` 标记为废弃，并要求跨会话存储由应用层集成；因此 SQL 来源、候选、隐私版本和事实治理保留在业务层。
- 飞书使用 `FeishuChannelProperties`、`FeishuCrypto`、`FeishuInboundMapper`、`FeishuAccessTokenProvider`、`FeishuOutboundClient`。没有自建 HTTP 发送器、token 缓存、加解密算法或 CardKit。
- SDK 示例 Controller 的内存去重与直接 Gateway 执行不替代 SQL 任务事务。应用回调保留身份绑定、强制签名/租户检查和持久受理，执行复用已有 TaskService / GovernanceService。
- SDK 文本发送返回 void，业务错误码只日志记录；不返回可核验的 message_id/幂等 UUID。正常结束仅标记 `submitted_unverified`，异常标记 `unknown`，不自动重发。卡片、表单和可靠回执查询等待框架支持。完整配置与验证边界见 [M4 记录](m4-validation.md)。

## M5 的交付能力复用

部署使用 Spring Boot profiles 区分消费者，Flyway 执行一次性迁移，Actuator 提供健康检查，configtree 读取挂载 secret；MCP 凭据引用经 Spring Environment 解析后交给原生客户端。只补充角色约束、现有 SQL 事实的受认证统计，以及使用 Docker Compose / pg_dump / pg_restore 的验收脚本。没有另建部署调度器、密钥库、MCP 协议或飞书发送器。配置和证据边界见 [M5 交付记录](m5-validation.md)。

[M5b](m5b-admission.md) 在已有 `TaskService.submit` 增加持久化受理检查，用 Spring 事务、MyBatis 和 PostgreSQL 行锁协调暂停与提交；排空报告直接查询既有任务、通知、记忆事实。它控制业务 API 和飞书的新任务入口，不替代 AgentScope 原生执行、状态恢复或审批，也没有新增后台排空调度器。已有任务沿用原 Worker，只有新 Turn 受理被暂停。

[M5c](m5c-backlog.md) 用现有 MyBatis 查询业务积压并按 ID 分页，受理、排空统计与明细均沿用现有 Bearer 身份。查询只补充业务事实定位，后续操作继续使用原审批、取消、通知和记忆流程；没有增加 AgentScope 执行层或数据库表。

[M5d](m5d-preflight.md) 只是已有运维 API 的只读调用和文件留档，使用 Python 标准库，与现有部署/验收脚本一致；没有增加 Java 运行依赖、AgentScope 包装协议或自动切换框架。

[M5e](m5e-restore-privacy.md) 使用 PostgreSQL 只读事务导出隐私元数据，标准库脚本核对私有快照；`pg_dump` / `pg_restore` 仍负责原生备份恢复。脚本只判断提供的证据，不增加运行时恢复引擎、遗忘重放或新的 AgentScope 抽象。

## M6a 的日期与基准复用

日期、区间、时区和 DST 处理使用 JDK `java.time` / `ZoneRules`，不自建日历库。旧 Python 仅用于显式离线生成回归样本，Java 构建读取冻结资源；未新增 Python 运行桥接、MCP 协议客户端或工具框架。该阶段紫微算法尚未选定和启用，后续工具继续接入 AgentScope 原生 Toolkit。版本差异与边界见 [M6 记录](m6-validation.md)。

## M6b 的历法复用

公历/农历转换复用固定的 `cn.6tail:lunar:1.7.7`，不实现新的通用历法库。逐日对照发现两处旧算法月界差异，只在薄适配层保存明确的兼容规则；原库 jar 与原始测试期望不修改。许可证随包保留，详细证据见 [M6b 历法记录](m6b-calendar.md)。历法阶段未包含本命与流运算法，未引入第二套 Agent、MCP 或 Skill 实现。

## M6b 的固定排盘算法

固定业务算法从已锁定的 MIT x-iztro 0.4.0 源码移植，星曜常量直接提取并记录来源，不新增算法运行桥接。历法继续复用 lunar-java，日期/时区使用 JDK，JSON 使用项目现有 Jackson；AgentScope 不提供紫微算法。M6c 已通过现有 `GovernedTools` 注册原生 `ToolBase`：`ToolValidator` 校验结构、`ToolResultBlock.suspended` 暂停、原生 `AgentState` 与已有 Interaction 恢复。Artifact、scope、能力步骤回执继续复用现有业务服务，无新状态库或 Skill/MCP/飞书协议。详见 [领域接入](m6c-domain.md)。
