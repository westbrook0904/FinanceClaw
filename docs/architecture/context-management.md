# 上下文生命周期与预算

本文描述当前 Java ReAct 主链路。一次 Turn 可以包含多次模型和工具调用，也可以暂停等待审批；conversation 串行接续多个 Turn。

## 长期记忆固定到 Turn

受理事务在用户隐私共享锁下读取当前人工画像和最多 16 条已确认事实，格式化正文并插入一次 `memory.turn_snapshots`。空记忆也保存空快照。后续模型调用、执行重试、进程重建和审批恢复都使用同一正文，不再重新读取画像或重新召回事实。

长期记忆经 `onSystemPrompt` 注入，每个 attempt 只加载一次；它不进入原生消息历史，也不交给压缩模型。普通画像编辑和新事实确认在下一 Turn 生效。遗忘会提升 epoch、删除快照和工作上下文，并阻断旧执行；撤权由授权版本检查独立阻断。Turn 完成后，下个 Turn 重新采样自己的长期记忆。

## 会话工作上下文

新 Turn 读取同 owner、conversation、隐私 epoch、release 和授权版本下，最新已完成且检查点版本匹配的工作投影。它包含用户问题、assistant 回答、工具名称/参数、工具结果或文件引用，以及必要的旧历史摘要。没有固定“只取最近 8 个问答对”的运行路径。

`agent_runtime.work_contexts` 每个 Turn 一行：`payload` 保存按派发预算管理的消息投影，`transcript` 保存本 Turn 在压缩前的完整消息轨迹；先前 Turn 的轨迹留在各自行中，不在每一轮重复复制完整 conversation。大工具正文单独保存为 JSON 文件，因此轨迹中的结果也使用文件引用。SDK 检查点和此投影在同一执行栅栏事务提交，等待/失败或尚未封存的 attempt 不成为下一 Turn 的已完成历史。最终回答追加后的持久投影可以暂时超过输入工作预算，由输出预留承接；下一次模型派发前再压缩和检查输入，并非存储行始终限制在 24 KiB。

## 各块预算

配置前缀为 `financeclaw.context`，环境变量使用 `FINANCECLAW_CONTEXT_…`，例如 `FINANCECLAW_CONTEXT_WORK_CONTEXT_BYTES`。

| 配置 | 默认 | 边界 |
| --- | ---: | --- |
| `system-bytes` | 8 KiB | 固定系统规则及 Skill 目录，扣除单列长期记忆 |
| `long-memory-bytes` | 16 KiB | 本 Turn 完整画像与证据；不静默裁剪 |
| `work-context-bytes` | 24 KiB | 摘要、近期问答、当前请求和工具消息合计 |
| `current-input-bytes` | 8 KiB | 当前用户问题，属于工作上下文子预算 |
| `tool-schema-bytes` | 16 KiB | 实际可用工具的定义和参数 Schema |
| `tool-result-bytes` | 8 KiB | 单个完整 ToolResultBlock，包含 metadata；超限生成文件 |
| `summary-bytes` | 2 KiB | `goals/tools/evidence/nextSteps` JSON |
| `compact-at-percent` | 80 | 工作上下文达到该比例才触发压缩 |
| `retained-rounds` | 1 | 至少保留最近的完整问答或工具批次 |

当前采用保守 UTF-8 字节估算，不声称等于供应商 tokenizer。实际模型窗口不足时会进一步收紧工作上下文容量；最终 `BudgetedTransport` 还会检查包含所有消息、工具、JSON 封装和输出预留的完整 HTTP 请求。必需内容无法容纳时明确失败，不截断当前请求或拆开工具配对。

## 接近容量时压缩

模型派发前先归档过大结果，再记录原始工作消息。达到阈值后，较早的完整问答和已结束工具批次交给摘要器；当前用户输入、最近一轮及未完成/审批中/失败工具配对保留。单个 Turn 的较早成功工具批次同样可以压缩。

真实模型模式使用同一受治理模型生成严格 JSON，字段是 `goals`、`tools`、`evidence`、`nextSteps`。摘要请求没有可用工具，不携带主对话 system 或长期记忆，按完整工具批次拆分输入，并计入本 Turn 模型调用次数。Qwen 摘要逐请求关闭 thinking，不改变主对话配置；摘要器只接受文本 JSON。输出格式、大小和实际压缩效果都需要通过检查；失败不能换成空摘要继续。离线模式仅使用确定性原文摘录。

摘要以明确标记的 USER 背景数据消息进入工作上下文，不能修改权限或审批状态。工具文件引用会确定性保留；引用过多时归档成可逐层读取的索引文件，摘要只保留小索引引用，防止长期会话因引用无限累积超预算。原始轨迹和文件仍可核对。

## 大结果文件

SDK 的 `PostActingEvent` 在结果进入观察上下文前替换大回执；`onReasoning` / `onModelCall` 再检查有效上下文，覆盖本地工具、MCP、Skill 和检查点恢复。上下文中只保留描述、产物 ID、SHA-256、字节数、下载入口和范围读取说明。

文件是 SQL 归档中的完整 JSON，使用受治理下载提供实际文件，不暴露服务器目录或任意路径。模型通过 `read_artifact_range` 每次读取最多 512 个 UTF-8 字节，用 `nextStart` 接续；HTTP `/v1/artifacts/{id}/range` 单次最多 16 KiB，`/download` 下载完整 JSON。大字符串也能分段读取，不要求原结果必须是数组。

读取检查 owner、隐私 epoch、当前原工具权限和 release。普通业务产物仍保留原有双权限门禁；大结果文件无需再额外授予 `artifact:read` 才能读取本来有权接收的工具结果。索引不授予目标文件权限，每个目标文件读取仍独立校验。当前单文件上限 2 MiB，超过时明确返回 `artifact_too_large`；归档失败不会返回伪造的成功引用。

源码入口：[运行器](../../src/main/java/com/financeclaw/infrastructure/agentscope/execution/TaskAgentRuntime.java)、[工作上下文中间件](../../src/main/java/com/financeclaw/infrastructure/agentscope/context/WorkingContextMiddleware.java)、[压缩算法](../../src/main/java/com/financeclaw/infrastructure/agentscope/context/WorkingContextCompactor.java)、[长期记忆](../../src/main/java/com/financeclaw/app/memory/MemoryService.java)、[文件读取](../../src/main/java/com/financeclaw/app/artifact/ArtifactService.java)。
