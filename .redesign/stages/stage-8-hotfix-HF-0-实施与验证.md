# Stage 8 Hotfix HF-0：固定原生调用方式——实施与验证

日期：2026-09-09。基线：`003ae4a`。状态：**HF-0 完成；HF-1／HF-2／HF-3 未实施。**

本阶段确认顶层 ReAct 内调用 subagent／workflow 的原生行为，交付可复现探针、回归测试、
新发布预留和旧路径退出清单。没有替换生产 DelegationTool，没有切换 BFF 或部署／迁移数据库。

## 1. 交付与验证结果

| 交付 | 位置／结果 |
|---|---|
| 合成模型驱动的真实原生图 | [`graphs.py`](../../experiments/stage8_hotfix/graphs.py)：顶层 create_agent、Agent Worker、HITL Agent Worker、StateGraph Workflow |
| 本地图验收 | [`local_probe.py`](../../experiments/stage8_hotfix/local_probe.py)：9/9；每次 resume 前重新装配图、保留原 checkpointer |
| 原生 HTTP 验收 | [`native_probe.py`](../../experiments/stage8_hotfix/native_probe.py)：9/9；真实独立 Agent Server，9 个顶层 thread、18 个原生 Run、0 个额外 child thread/run |
| 共享行为断言 | [`checks.py`](../../experiments/stage8_hotfix/checks.py)：interrupt 绑定、ToolMessage、实际节点计数、输出／命名空间隔离 |
| HF-0 回归 | [`test_native_subgraphs.py`](../../tests/stage8_hotfix/test_native_subgraphs.py)：14 项，另覆盖错误 ID、错误回答、环境凭据隔离和禁止远程 child 依赖 |
| 相关回归 | HF-0＋既有 Workflow 图＋批次工具测试：**27 passed，6.46 秒** |
| 代码检查 | Ruff check／format 通过，11 个 Python 文件 |
| 发布与退出清单 | [`release-plan.json`](../../experiments/stage8_hotfix/release-plan.json)：仅预留，未写入生产 Catalog |
| 正式证据 | [`hf0-native.json`](../evidence/stage8-hotfix/hf0-native.json)：实际依赖版本、源码 SHA-256、逐场景原生 ID／计数／断言 |

全部模型决策是可重复的合成实现；LangChain／LangGraph 图执行、工具调用、中断、检查点以及
HTTP Run API 都使用实际框架实现。合成副作用仅写入探针计数文件，没有真实渠道发送或外部业务动作。

## 2. 冻结的调用契约

### 2.1 只在顶层创建原生 Run

```text
外部调用方创建顶层后台 Run
  → 顶层 create_agent 的模型节点决定调用 Tool
  → Tool 等待已装配 Worker 子图的 ainvoke
  → 子图返回公开结果
  → ToolNode 生成原 tool_call_id 对应的 ToolMessage
  → 顶层模型继续下一轮 ReAct 或输出最终回答
```

- 子图编译使用 `checkpointer=None`，继承根运行时提供的持久化；每次工具调用隔离状态。
- Tool 显式传递 `runtime.config`，保留原生 callback、checkpoint namespace 和执行上下文。
  不新建 thread，不手工重写 checkpoint namespace，不在 Tool 内创建 SDK client。
- 子图接收代码构建的任务输入及调用范围；根身份继承，原 root tool_call_id 绑定到子图 context。
  子 Agent 只接收本次任务消息，Workflow 通过输入／输出映射使用独立 state schema。
- Worker 在装配阶段创建一次；每次调用不会构造新的应用 bootstrap、Catalog 或数据库资源。
- HTTP 探针只注册 `hf0_orchestrator_v1`。没有人工中断时，两个串行子图只需要一个顶层 start。
  人工中断后，每次回答创建一个新的顶层 resume attempt，thread 和业务 root 保持不变。

实现方式符合官方 [Subagents](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents)
与 [Subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)的调用模型；上述版本行为另由本阶段探针确认。

### 2.2 人工交互只恢复原顶层等待点

资料提问、Workflow 显式审批和子 Agent 原生 HumanInTheLoopMiddleware 均能把 interrupt
传播到顶层。外部只提交 `Command(resume={原生_interrupt_id: 已验证回答})`，不恢复独立 child。

HTTP 使用原顶层 thread、保存的根 checkpoint、固定 operation metadata 和 `multitask_strategy="reject"`；
每次完成后核对该 thread 的全部原生 Run，与探针显式创建的 start／resume 清单完全一致。
`durability="sync"` 是本探针采用的调用参数，不以 dev/inmem 的成功代表生产数据持久化。

业务恢复前仍须由 HF-2 的 BFF 验证归属、当前交互 revision、发布声明、动作和回答 Schema；
LangGraph 的可恢复能力不替代这些业务校验。

## 3. 实测发现及对后续实施的约束

### 3.1 Tool 内的子图不可直接查看完整嵌套 state

本机版本下，顶层 `get_state(subgraphs=True)` 的 pending task 能暴露子图 interrupt，
但 `task.state` 没有完整子图状态。不能据此判断“没有子图运行”或要求 BFF 遍历 child state。

顶层 messages 中仍有尚未收到 ToolMessage 的复合 tool_call；在独占交互批次约束下，
可以将它与顶层暴露的 interrupt ID 绑定。资料／Workflow 自定义载荷还包含原 root call ID、
固定 Worker release、point ID、revision 和 deadline。

原生 HITL 载荷只有 `action_requests`／`review_configs`，没有应用的 Worker release 或 root call ID。
HF-1／HF-2 必须把根上唯一待返回的复合工具与根发布内的 Worker／叶子工具声明联合核对，
再校验原生动作名和参数。无法唯一绑定时拒绝恢复；不得信任 description 文本来决定授权。

### 3.2 连续提问可能复用同一个父 checkpoint

`two_questions` 场景实际观测到：

1. 第一个问题暂停顶层；第一次 resume 恢复该子图，随后产生第二个问题。
2. 第二个问题拥有新的 interrupt ID，仍绑定原顶层 Tool call。
3. 父 checkpoint ID 没变，父 checkpoint metadata 中的 `run_id` 仍指向首次 start，
   而当前完成到中断位置的原生 attempt 已经是第一次 resume。
4. 用第二个 interrupt ID 在原顶层 thread 恢复，最终得到包含两次回答的工具结果和顶层回答。

因此不能把 `checkpoint.metadata.run_id == 当前 attempt` 作为所有等待位置的必要条件，
也不能仅用 checkpoint ID 为交互去重。交互身份必须包含原生 interrupt ID 和 BFF 交互版本。

HTTP 探针通过独占 thread、精确 Run 回执及 operation、完整原生 attempt 清单和原根身份
验证状态所属链；它不直接复用现有 Coordinator 的 observation 算法。
HF-2 必须在同根独占写入和 revision 校验下保存“当前 attempt＋父 checkpoint 锚点＋当前 interrupt”，
核对前驱和恢复命令，观察提交前再次确认当前绑定没有变化；不能仅查询 thread 最新值后跨根／跨轮认领。
最终根输出提交时，本探针仍要求根完成 checkpoint 属于最后的成功 attempt。

### 3.3 恢复会重入外层工具，但不重跑已完成子图节点

`workflow_approve` 的实际计数：

| 位置 | 次数 |
|---|---:|
| 顶层复合 Tool 入口 | 2 |
| Workflow prepare 节点 | 1 |
| Workflow approval 节点入口 | 2 |
| Workflow finish／已批准合成副作用 | 各 1 |
| 顶层 Tool 返回 | 1 |

HF-1 的复合 Tool 包装层必须可重放；不能在子图调用前发送通知、重复创建副作用或重置预算。
实际叶子动作仍需要自己的业务幂等保护。`repeat_agent`／`repeat_workflow` 还验证：第一次已完成的
调用不随第二次调用的 resume 重跑，第二次调用使用独立原生命名空间。
框架重入规则参见 [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)。

### 3.4 不能把任意字典键当作 interrupt ID

本机版本用 32 位十六进制原生 ID 识别 resume ID map。不存在但格式正确的 ID 不会应用到
当前问题；普通字符串键会被当作整个 resume payload，探针中的回答 Schema 将其拒绝。
HF-2 必须使用从原生观察中绑定的确切 ID，不能接受客户端任意组装的 resume map。

## 4. 逐场景结果

本地图与真实 HTTP 均通过以下场景：

| 场景 | 顶层 Tool 次数 | 用户 resume | 原生 Run | 合成副作用 |
|---|---:|---:|---:|---:|
| Agent → Workflow 串行调用 | 2 | 0 | 1 | 0 |
| 子 Agent 提问 | 1 | 1 | 2 | 0 |
| 子 Agent 连续两次提问 | 1 | 2 | 3 | 0 |
| Workflow 批准 | 1 | 1 | 2 | 1 |
| Workflow 拒绝 | 1 | 1 | 2 | 0 |
| 子 Agent 原生 HITL 批准 | 1 | 1 | 2 | 1 |
| 子 Agent 原生 HITL 拒绝 | 1 | 1 | 2 | 0 |
| 同一 Agent 两次调用，第二次提问 | 2 | 1 | 2 | 0 |
| 同一 Workflow 两次调用，第二次审批 | 2 | 1 | 2 | 1 |

这里的 Tool 次数指成功返回的顶层复合调用数量，不包括恢复时包装函数的重入。
9 个场景共 9 个顶层 thread、9 次 start、9 次 resume、18 个原生 Run。
没有新增 child HTTP thread/run；顶层最终回答均在相应 ToolMessage 返回后产生。

## 5. 为 HF-1 冻结的新发布与退出清单

以下只写入实验发布计划，**尚未进入生产 Catalog 或 langgraph 配置**：

| 发布对象 | 当前版本 | 新版本预留 | 新调用名／根 graph ID |
|---|---|---|---|
| finance_agent | 1.4.0 | 1.5.0 | `finance_agent_v1_5_0` |
| market_research_agent | 1.2.0 | 1.3.0 | `call_agent__market_research_agent` |
| ziwei_doushu_agent | 2.0.0 | 2.1.0 | `call_agent__ziwei_doushu_agent` |
| portfolio_review | 1.0.0 | 1.1.0 | `call_workflow__portfolio_review` |

新 Worker 不独立注册为供业务创建 Run 的 graph；工具版本随对应 Worker 发布冻结。
根快照固定全部可达 Worker 和允许的工具声明，保留现有领域输入／输出语义。
紫微的候选开关、文本输出和数据分类约束不因本计划改变。

退出步骤按 HF-1～HF-3 执行：移除新路径的 DelegationTool／handoff／child execution／result delivery，
同步 `/agent`／`/workflow` 的工具名称映射；保留旧图版本排空已有根；无活动 Turn 的会话才显式切新版本和 thread；
保留 Journal、审计、通知和 8C legacy fence；最后退出 Coordinator 正式入口。
不在 HF-0 修改旧图 ID、取消已有任务或删除数据。

## 6. 复现与阶段边界

运行说明和隔离配置见 [探针 README](../../experiments/stage8_hotfix/README.md)，依赖固定版本见
[`requirements.txt`](../../experiments/stage8_hotfix/requirements.txt)。

```bash
.venv/bin/python -m pytest tests/stage8_hotfix \
  tests/stage4/test_portfolio_review_graph.py tests/stage6fix/test_batch_tools.py -q

.venv/bin/python -m experiments.stage8_hotfix.run \
  --report .redesign/evidence/stage8-hotfix/hf0-native.json
```

Python `3.13.15`；LangChain `1.3.18`、langchain-core `1.6.1`、LangGraph `1.2.11`、
SDK `0.4.4`、Agent Server API `0.13.3`、runtime-inmem `0.33.3`、checkpoint `4.2.0`。

本阶段不验证真实 LLM 选工具质量、正式领域工具／预算／权限接线、BFF Journal／Webhook、
真实渠道投递或生产持久运行时重启。Graph 重建时仍使用同一个 InMemorySaver，不称为跨进程恢复。
这些范围继续按 [Hotfix 实施方案](./stage-8-hotfix-实施方案.md#9-分阶段实施与验收)的后续阶段验收。
