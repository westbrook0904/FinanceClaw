# M2：工具治理、交互恢复与请求预算

> **历史记录**：本文保留当时的设计、命令与验证结果，不代表当前独立应用的部署结构或最新测试结论。当前操作见[运行说明](../operations.md)，新库结构见[初始化说明](../architecture/fresh-schema.md)；历史类名、版本号和外部导出命令仅用于理解证据。

日期：2026-10-15。**本页的测试结果为 M2 冻结证据；当前业务增量和完整回归见 [M3](m3-validation.md)。** M2 在 M1 的任务生命周期上接入框架原生事件、审批、澄清、模型与 MCP。实现以[源码指纹与测试结果](evidence/m2-results.json)为准；[M0](m0-validation.md)和 [M1](m1-validation.md)报告作为历史阶段证据保留。框架能力取舍见[复用审查](framework-reuse-review.md)。

## 1. 实现与约束

- **固定发布**：受理时冻结 release hash、授权 revision 和有效 scopes。发布包含 SDK 版本、行为版本、模型主备参数、工具授权、调用预算与 MCP 契约；恢复沿用原 hash。当前实例可同时持有已加载版本；重启后未配置/加载的旧版本明确 `release_unavailable`，不会切换到新版本继续执行。变更工具实现或系统提示时必须提升行为版本。
- **授权与身份**：身份只来自开发 Bearer principal；授权变更用版本与幂等键串行提交。每次模型/工具 dispatch、恢复、结果归档、完成提交都复查授权。执行时钟取 PostgreSQL，身份通过原生 `RuntimeContext` 注入，工具 schema 不接受身份字段。
- **审批/澄清**：问题保存原 attempt、状态版本、发布、授权版本、工具调用和动作摘要。有效期 15 分钟。审批用 `ConfirmResult`，澄清用原生成功 `ToolResultBlock`。重复相同回复返回已受理回执，改变已答内容冲突；同批所有问题回答后只排队一次。新 attempt 从原快照恢复，旧槽保持不变。
- **写工具**：`create_report` 只在本地业务表创建报告记录；报告写入与逻辑调用回执同事务，按 Turn/call ID 幂等。未知效果进入阻断，取消也不能绕过核对。M2 不开放任何外部写 MCP。
- **结果归档**：完整 JSON 保存到 PostgreSQL Artifact 表，最大 2 MiB，记录规范 JSON UTF-8 内容的 SHA-256。引用只含 ID/hash/bytes；回读重新检查 owner、当前 artifact 权限与发布是否存在。数组分页 1–100 项且数据不超过 16 KiB；对象整体返回，忽略数组偏移，过大则明确拒绝。
- **共同治理入口**：本地工具、MCP 均继承 `ToolBase`。`GovernedTools.narrow()` 只允许收窄 scopes，子调用共享持久的根 Turn 模型/工具次数预算；M3 的实际子 Agent 和 Workflow 业务场景尚未接入。

新的 V3 迁移包括 grants、turn_policies、interactions、artifacts、tool_calls 和 reports。Artifact 与 AgentState 表都进入 readiness 检查。旧 M1 Conversation 的新授权默认为空，历史仍可读；旧活动发布缺失时阻断，需要显式处理，不自动改写发布或重放任务。

## 2. 使用入口

按 [README](../../README.md) 启动。默认 `offline` 模型仅回显正文，便于无密钥验证服务；本地工具已注册，但离线回显不会自行选择工具。确定性审批/澄清及模拟 MCP 的可重复入口是第 4 节测试。

接入实际兼容 OpenAI 的模型时，配置以下环境变量，再重启进程：

```bash
export FINANCECLAW_MODEL=<provider-model-name>
export FINANCECLAW_MODEL_BASE_URL=<provider-base-url-including-v1>
# FINANCECLAW_MODEL_API_KEY 通过本地环境或密钥管理注入
export FINANCECLAW_MODEL_CONTEXT_WINDOW=<actual-provider-context-window>
export FINANCECLAW_MODEL_INPUT_LIMIT=<actual-provider-input-limit>
export FINANCECLAW_MODEL_OUTPUT_RESERVE=2048
export FINANCECLAW_MODEL_SAFETY_MARGIN=512
export FINANCECLAW_MODEL_THINKING=false
scripts/mvn.sh spring-boot:run
```

请使用供应商实际窗口与输出限额；开发默认值不代表任意模型都支持该容量。备用模型通过 `FINANCECLAW_FALLBACK_MODEL`、`FINANCECLAW_FALLBACK_BASE_URL` 和同名前缀的 `CONTEXT_WINDOW / INPUT_LIMIT / OUTPUT_RESERVE / SAFETY_MARGIN / THINKING` 配置，默认关闭；本阶段主备共用注入的 API key。

工具包括：`request_city`（根层资料澄清）、`search_catalog`（明确标为本地 fixture 的示例酒店）、`create_report`（需要审批）、`read_artifact`。MCP 只在发布提供固定 URL、工具名和 schema 时初始化；本阶段通过模拟发布验证，不默认连接酒店或其他真实服务。

| API | 行为 |
| --- | --- |
| `GET /v1/conversations/{cid}/grant` | 当前授权 revision / scopes |
| `PUT /v1/conversations/{cid}/grant` | `{"revision":1,"scopes":["catalog:read","artifact:read"]}`，必须带 Idempotency-Key |
| `GET /v1/conversations/{cid}/turns/{tid}/interactions` | 只读问题列表，审批展示报告标题，不暴露原生运行坐标 |
| `GET /v1/interactions/{id}` | 获取归属当前主体的问题 |
| `POST /v1/interactions/{id}/responses` | 返回 202 与持久回复回执；必须带 Idempotency-Key |
| `POST /v1/conversations/{cid}/turns/{tid}/interactions/{id}/answer` | 同一回复用例的嵌套路由 |
| `GET /v1/artifacts/{id}?start=0&limit=20` | 有界、重新授权的回读 |

审批正文：

```json
{"kind":"approval","revision":1,"decision":"approve","action_hash":"从当前问题回传的摘要"}
```

拒绝使用 `decision: "reject"`。澄清只提交答案，不能带 approval 的 decision/action_hash：

```json
{"kind":"input","revision":1,"answer":{"city":"上海"}}
```

`reason` 可选且不超过 2000 字符。错误主体返回 404；旧版本、过期、已取消、授权变化或幂等冲突返回 409；类型/schema/未知字段错误返回 422。回复回执固定表示该答案已持久受理，最终业务状态继续查询 Turn；相同已受理回复的重放不会再次执行。

保留源系统回复 URL、审批 `approve/reject` 和输入分型。Java 回执使用 `AnswerReceipt`，尚未复制 Python 的所有投影信封；渠道切换前仍需 M4/M5 的端到端兼容验收。

## 3. 上下文、流与资源边界

历史只投影最近至多 8 个完成 Turn，且必须同会话、同发布、同授权版本；只取完整用户/助手对，最多占主模型输入预算的四分之一且不超过 8 KiB。当前原始用户请求及恢复证据不裁剪。最终发送载荷另经 `BudgetedTransport` 检查，超限在网络请求前拒绝；不会临时扩大限额。

文本/JSON 使用 UTF-8 字节数作为保守 token 上界，无 Python tokenizer 缓存依赖。外链图片、音频等不能通过 URL/字节长度推断供应商 token 成本，当前明确拒绝，不能视为已支持视觉/音频输入。中文、大酒店结果、工具 schema、tool calls、response schema 和多模态拒绝路径均有测试。

SDK `GenerateOptions` 仅向名称包含 Qwen 的模型传递 `enable_thinking`；普通 OpenAI 请求不携带这个专有字段，其他模型启用该开关会在配置校验时拒绝。usage 缺失时仍依赖发送前预算；负数、超过预留输出或窗口的 usage 被拒绝。无正文/工具未完成不会标 completed。SDK 原生 fallback 使用备用模型自己的窗口与参数，不复用主模型预算。

模型和工具执行的原生最大尝试数均为 1；没有自动重跑整条任务。只读 MCP 的明确 I/O/Timeout 异常允许至多 1 次额外重试，100ms 起始退避；契约、授权和业务错误不重试。网络超时由 SDK 配置，整个 attempt 仍受 Worker 时限与租约保护。

SSE 复用原生事件和 Spring/Reactor，存储安全投影后再提供游标回放。`FinalAnswerFilterMiddleware` 在确认本轮无工具调用后释放正文；不承诺即时逐 token 转发。tool_* 事件只包含固定名称和状态。生产默认关闭 SDK/MCP 的原始日志，HTTP 错误只给固定错误码。

## 4. 验证与复现

```bash
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/collect-evidence.py
# 仅重跑 M2 的数据库场景（仍会先运行单元测试）
scripts/mvn.sh -B -ntp verify -Dit.test=GovernanceIT
```

本次干净构建 **83 项测试通过，0 失败、0 错误、0 跳过**；M0 28 项、M1 23 项、M2 32 项。打包与 Spotless 检查通过。结果记录在[结果报告](evidence/m2-results.json)，跨 JVM 退出证据见[进程记录](evidence/m2-process-exits.json)。测试入口：[GovernanceIT](../../src/test/java/com/financeclaw/m2/GovernanceIT.java)、[ModelPolicyTest](../../src/test/java/com/financeclaw/m2/ModelPolicyTest.java)、[MockMcp](../../src/test/java/com/financeclaw/m2/MockMcp.java)。

| 场景 | 实际证据 |
| --- | --- |
| 审批/澄清重启恢复 | 各 2 个独立 JVM；第一进程挂起退出，第二进程从 PostgreSQL 恢复 |
| 幂等与批量审批 | 并发重复回复只排队一次；两项审批收齐后执行两个不同逻辑调用 |
| 拒绝/撤销/旧问题 | 无写入；过期、取消、状态版本变化、跨主体、错误 schema 均拒绝 |
| 发布与执行身份 | 受理后更新发布仍使用原版本；缺失版本 blocked；DB 时钟和服务端主体固定 |
| 预算继承与权限收窄 | 恢复累计模型次数；子工具集合不能扩权，调用计入同一根 Turn |
| Artifact | 完整保存、hash、分页、对象偏移、过大结果、跨租户及撤销后拒绝回读 |
| 原生 MCP | loopback HTTP 上真实 SDK initialize/list/call；schema 改变不发起工具调用 |
| 完整请求预算 | 实际 OpenAI adapter 与最终 JSON 载荷，UTF-8 fallback、thinking、usage、空正文、备用模型限额 |
| 事件脱敏 | 中间轮次文本被过滤；工具事件不含参数、原始结果、异常或 thinking |
| M0/M1 回归 | CAS、删除墓碑、同主体跨租户隔离、租约、完成回执、取消、HTTP/SSE 重连继续通过 |

这是本地自动化和模拟集成证据。真实供应商模型质量、真实酒店 MCP、外部写操作核对、飞书、对象存储、生产身份系统、CI 远端执行与 M3–M6 尚未验收。M2 的 MCP 契约采用固定 schema 比较，仅消除 SDK 根 JsonSchema 的缺省 null 字段；后续 M3 已按原生框架优先决策移除本地契约比较，改为 Toolkit 原生工具发现与参数校验，详见 [M3 当前说明](m3-validation.md)。
