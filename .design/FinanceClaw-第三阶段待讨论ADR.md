# Stage 3 ADR 状态摘要

> **状态日期**：2026-09-02
> **当前架构基线**：顶层 AGENT + 固定 WORKFLOW + DIRECT
> **历史快照分支**：`codex/history-before-framework-reuse-20260902`（仅本地）

## 当前有效决议

### ADR-P3-F-001～004：Context、Memory 与 Agent 治理基础

**状态：保留。**

- `InvocationContext` 保存身份、租户、deadline、trace 等执行元数据；
- `ContextSnapshot/Projection` 保存一次模型调用可见的受控上下文；
- Memory 与执行 checkpoint 分离，只保存允许跨请求复用的事实；
- 用户输入、Memory 和 Tool output 都是 DATA，不能提升为系统指令；
- Tool 调用必须经过 Capability Catalog、Policy、Invoker 和稳定 Result/Error 边界。

旧 Minimal Explore 的循环实现已删除，但其次数预算、Observation 有界化、工具 scope、
单写者和 fail-closed 语义将作为新 AgentRuntime 的验收要求保留。

### ADR-P3-F-005：Agent write-ahead 模型调用计数

**状态：原则保留，机制改由 LangGraph 实现。**

每次模型调用前仍需先占用受信任的 run/turn budget，防止崩溃恢复后无限重复调用。旧模型
reservation/slot/incarnation 协议不再保留；计数进入 typed graph state/checkpoint，并由
FinanceClaw Agent middleware 强制执行。

### ADR-P3-F-006：prepared generation reservation

**状态：模型侧已废止。**

该方案与自研 ModelGateway、Provider retry/fallback 和 incarnation fencing 绑定。模型调用改由
LangChain 负责后不再使用。Capability WRITE 的 idempotency、equivalence group、Provider retry
与 fallback 安全继续有效，二者不能混为一谈。

### ADR-P3-F-007：顶层 Agent、确定性分派与固定 Workflow

**状态：接受，旧模式代码已删除。**

```text
显式 Capability → DIRECT
显式 Workflow   → WORKFLOW（版本化 StateGraph）
无显式目标      → AGENT（顶层 ReAct/Agent loop）
```

不再使用 LLM 选择 FAST/PLAN，不再让 LLM 构造复杂 PlanDraft。`ExecutionMode`、RouteDecision、
LLMRouter、LLMPlanner、PlanDraft 与旧 ExplorationEngine 已从 `main` 删除。

### ADR-P3-F-008：LangChain 模型运行时复用

**状态：接受，旧模型栈已删除，薄适配层待实现。**

LangChain 负责模型 Provider adapter、message/tool-call、structured output、普通 retry/fallback、
stream 和 usage。FinanceClaw 保留 ModelProfile、Policy、Context 出站治理、deadline/budget 和
Trace/Event callback bridge。模型不注册为 Capability，也不复用 Capability Provider Fabric。

### ADR-P3-F-009：LangGraph 编排运行时复用

**状态：接受，旧 Plan/DAG 栈已删除，薄适配层待实现。**

LangGraph 负责 Agent/Workflow 图执行、条件分支、并行、节点 retry、checkpoint、resume、
interrupt、subgraph 和 stream。FinanceClaw 保留 Workflow Registry、CapabilityNodeAdapter、
Policy、Context/Memory、审批业务语义、Run Index 与稳定外部 Result/Event。

## 本次删除边界

已从 `main` 删除：

- `harness-routing`；
- `harness-planning`；
- `harness-model`；
- `harness-agentic`；
- `harness-execution`；
- `harness-state`；
- 相应 Plan/Model/Route/Explore Contracts、Bootstrap API 与历史测试；
- 旧 `real-use` ModelGateway Gate。

继续保留：

- Contracts、SPI、Registry、Selection、Policy；
- Context、Memory、Observation；
- CapabilityInvoker、Provider retry/fallback 与 WRITE safety；
- Trace、Provider Events、Plugin lifecycle、Direct Invocation。

## 下一轮需要共同冻结的设计

1. `RequestTarget` 如何表达 CapabilityTarget、WorkflowTarget 与无目标 Agent；
2. `ModelProfile`、`ModelRuntime` Port 和 LangChain integration 的最小稳定面；
3. `AgentState` 中 message、context use、memory proposal、turn/action/observation 与 budget 字段；
4. `CapabilityToolAdapter` 如何把 LangChain tool call 安全落到 `CapabilityInvoker`；
5. `WorkflowDefinition/WorkflowVersion/GraphFactory` 的发布和兼容策略；
6. LangGraph checkpointer、`thread_id`、业务 `run_id` 与租户隔离；
7. interrupt/approval、stream/event、Trace 和错误归一化的桥接；
8. Python 3.14 与目标 LangChain/LangGraph 版本的兼容性 spike。

在这些接口冻结前，不直接写新的“大而全 AgentRuntime”。先做最小 spike，再以一条真实金融
Agent 任务和一条固定 Workflow 验证端到端闭环。
