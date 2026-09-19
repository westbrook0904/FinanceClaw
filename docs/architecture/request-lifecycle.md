# 一次请求如何完成

本文把产品行为对应到真实源码，适合调试任务状态或理解跨模块调用。公开请求示例见 [Turn 运行手册](../operations/turn-control.md)，模块归属见[包结构](package-layout.md)。

## 先区分三种身份

- **Conversation**：用户持有的会话，负责归属、历史和当前发布绑定。
- **Turn**：一次业务任务。用户补充信息或确认审批后，仍然继续同一个 `turn_id`。
- **Command / native run**：开始或恢复分别创建不可变命令；每条命令关联经核对的原生执行回执。

一次 Turn 可以经过多次人工恢复，因此不能把它简单等同于一个 native run。客户端提交 `message` 与幂等键，原生 thread、checkpoint 和 run 坐标由服务端管理。

## 1. 受理：先保存业务事实

HTTP 路由是 `POST /v1/conversations/{conversation_id}/turns`。调用链如下：

```text
api/http/routers.py: start
  → TurnService.start_turn
  → TurnAdmission.accept
  → 提交用户消息、Turn、start Command 与相关业务事实
  → 唤醒后台观察者
  → 返回 202、turn_id、revision
```

[TurnAdmission.accept](../../financeclaw/api/application/turns/admission.py)检查会话归属、根发布、身份权限与幂等键。在短事务内锁定会话，同一个会话只允许一个活动任务。同一幂等键与相同请求复用原结果；键被用于不同请求时返回冲突。

事务还固定本次执行的发布、上下文和授权。显式 `/skill` 选择在受理阶段解析并校验，保存为任务绑定，但技能正文的模型请求装配由图执行负责。失败或已取消任务之后的新 Turn 使用干净的原生 thread，持久化 Journal 仍保留。

**`202` 表示已持久受理，不表示模型已经开始或任务已经完成。** 受理事务本身不调用模型，也不提交原生图执行。

## 2. 提交：业务观察与图执行分工

[TurnLifecycle](../../financeclaw/api/application/turns/lifecycle.py)在 API 进程中扫描待处理任务、获取短期观察租约并续期。租约用于避免多个 API 副本同时提交冲突的业务观察结果，不是模型执行队列。

[CommandService.reconcile](../../financeclaw/api/application/turns/commands.py)处理原生命令：

| 命令状态 | 处理方式 |
| --- | --- |
| `prepared` | 在短事务中校验授权和预算，领取唯一发送权，进入 `sending` |
| `sending` / `uncertain` | 按原操作身份查找原生回执，查到后绑定；不能通过新命令 ID 重发 |
| 已绑定回执 | 观察原生运行与 checkpoint，推动业务状态 |

[NativeRuns.submit](../../financeclaw/api/application/turns/backend.py)在 SQL 事务之外调用 `runs.create`，开始操作提交输入，恢复操作提交绑定的 interrupt 回答和 checkpoint。API 使用进程内 LangGraph SDK；实际图执行由独立的原生 queue worker 完成。

业务库提交与原生任务提交不是同一个事务。发送超时无法直接判断“没有执行”，因此保留 `uncertain` 命令并核对原身份。观察租约可以重新领取，命令发送权不能因为租约到期而重新发放。

## 3. 执行：从固定发布装配 Agent

唯一公开根图在 [langgraph.json](../../langgraph.json) 注册为 `finance_agent`：

```text
agent_server/graphs/product.py: finance_agent
  → build_components
  → shared/releases/catalog.py: build_release_catalogs
  → AgentFactory.build
  → LangChain Agent + 工具 + 中间件
```

[AgentFactory.build](../../financeclaw/agent_server/agents/factory.py)按固定 Agent 档案装配模型、工具、技能及中间件。阅读时可按职责分组理解：

| 职责 | 关键实现 |
| --- | --- |
| 工具授权、审批与调用预算 | `middleware/middleware.py`、`middleware/execution_middleware.py`、`middleware/batch_middleware.py` |
| 长期记忆召回与上下文准备 | `middleware/memory_middleware.py`、`context/preparation.py`、`context/compaction.py` |
| 模型最终输入容量检查 | `context/planning.py`、`middleware/final_context.py` |
| Skill 显式准备、边界检查与请求投影 | `middleware/skills.py`、`skills/service.py` |
| 工具大结果归档与结构化回读 | `middleware/artifact_middleware.py`、`shared/artifacts/` |
| 子图调用与资料澄清 | `tools/subgraphs.py`、`middleware/worker_clarification.py` |
| 飞书可展示的工具状态 | `middleware/tool_progress.py` |

表中未标注 `shared/` 的路径相对于 `financeclaw/agent_server/`。这是阅读分组，实际装配顺序以工厂代码为准；不同中间件的 hook 阶段也不同，不能根据表格顺序推断执行顺序。

领域子图、MCP 和 Skills 共用根任务的治理边界。技能提供方法，MCP 提供外部工具，子图负责领域任务；能力扩展不能绕过授权或扩大根任务预算。

## 4. 等待与恢复：回答绑定到原问题

当图需要补充资料或审批时，原生 `interrupt` 保存可恢复的等待点。[NativeRuns.observe](../../financeclaw/api/application/turns/backend.py)核对运行身份并读取固定 checkpoint；[ResultService.apply](../../financeclaw/api/application/turns/results.py)保存 Interaction，将业务任务标记为 `waiting`。

用户通过 `/v1/interactions/{id}/responses` 提交回答，或在满足单一文本澄清条件的飞书单聊中直接回复：

```text
TurnInteractions.accept_response
  → 校验归属、问题 revision、类型、有效期和当前任务
  → 校验回答结构；审批还需核对 action_hash 与允许的决定
  → 在事务内保存回答、收窄后的授权与 resume Command
  → TurnLifecycle 提交恢复命令
  → 从绑定的 interrupt / checkpoint 继续同一 Turn
```

具体实现见 [interactions.py](../../financeclaw/api/application/turns/interactions.py)。重复回答只有在幂等键和正文都匹配时才复用；旧问题、已取消任务或不匹配的版本不能恢复当前执行。记忆候选的确认走独立记忆接口，不恢复业务 interrupt。

## 5. 完成：原生结果转成产品事实

原生运行提示 `success` 后，仍需检查固定 checkpoint 中是否存在 interrupt、剩余节点、任务错误及正确的执行身份。只有这些证据匹配，业务层才确认 `completed`。

[ResultService.apply](../../financeclaw/api/application/turns/results.py)在一个应用数据库事务内写最终 Journal、命令观察结果和任务终态。相关通知、历史索引意图与符合配置的记忆提取意图沿同一业务事务提交。最终正文写入成功后，客户端可通过任务快照或会话消息读取。

后续工作分属不同进程：

- `integrations` 投递通知、维护历史和记忆索引，见 [history.py](../../financeclaw/integrations/history.py)与[通知手册](../operations/notifications.md)。
- `memory_worker` 消费提取与整合工作，SQL 保存记忆事实，Store 维护检索投影，见[记忆后台任务](../operations/memory-outbox.md)。

用户收到最终答案与后台记忆索引完成是不同事件，应分别观察。

## 6. 进度、断连和取消

产品 SSE 发送 `turn.snapshot` 与心跳，重连读取最新 revision，属于任务状态投影。原生 `messages` / `custom` 流由 [TurnAnswerStream](../../financeclaw/api/application/turns/answer_stream.py)收集成受限展示内容，用于飞书进度和工具状态；它不负责决定任务完成。

客户端断开不取消原生执行。取消接口先保存意图，后台核对原生停止后进入 `cancelled`。不能把 HTTP 请求断开、单次流读取失败或取消请求已受理当作业务终态。

| 产品状态 | 如何理解 | 下一步 |
| --- | --- | --- |
| `accepted` / `queued` / `running` | 已受理、原生排队或正在执行 | 观察状态，排查时结合 API 与 Worker |
| `waiting` | 有待回答的持久交互 | 读取 `pending_interactions`，按当前版本提交回答 |
| `resuming` | 回答已受理，恢复待提交或待确认 | 继续观察同一 Turn |
| `cancelling` | 已记录取消意图，尚未确认停止 | 等待原生回执核对 |
| `blocked` | 当前缺少继续推进所需的确定事实或有效授权 | 查看原因，按 [Turn 手册](../operations/turn-control.md)处理 |
| `completed` / `failed` / `cancelled` | 业务终态 | 读取结果或原因；后续工作创建新 Turn |

状态集合见 [TurnStatus](../../financeclaw/kernel/turn_status.py)。故障恢复时先确认当前 Turn、Command 和 Interaction 的关系，再判断应回答、取消、恢复授权还是处理基础设施问题。
