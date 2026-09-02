# FinanceClaw 设计文档索引

> **当前原则**：运行机制优先复用 LangChain/LangGraph；FinanceClaw 自研重心是 Agent 记忆、
> 上下文、工具管理和受控调用。

## 当前有效基线

| 文档 | 定位 | 状态 |
|---|---|---|
| `FinanceClaw-LemonClaw-架构对齐分析.md` | LemonClaw 模块、框架复用与 FinanceClaw 边界 | 参考基线 |
| `FinanceClaw-顶层Agent与确定性Workflow-ADR讨论稿.md` | DIRECT/WORKFLOW/AGENT 与顶层 ReAct | **ACCEPTED** |
| `FinanceClaw-LangChain模型运行时复用-ADR讨论稿.md` | 用 LangChain 替代自研模型运行时 | **ACCEPTED** |
| `FinanceClaw-LangGraph编排运行时复用-ADR讨论稿.md` | 用 LangGraph 替代自研 DAG/State runtime | **ACCEPTED** |
| `FinanceClaw-第三阶段待讨论ADR.md` | 当前 ADR 状态、删除边界与下一轮问题 | 当前摘要 |
| `FinanceClaw-Agent-Foundation-一期实施说明书.md` | Context/Memory/Agent 治理语义来源 | 保留有效部分 |

## 当前代码状态

旧 Router、Planner/PlanDraft、ModelGateway/model provider adapter、ExplorationEngine、
ExecutionEngine/Scheduler/StateStore 已从 `main` 删除。删除前代码保存在本地历史分支
`codex/history-before-framework-reuse-20260902`。

当前代码只提供 DIRECT 领域核心；LangChain/LangGraph 适配尚未实现。下一步必须先冻结薄接口
与兼容性 spike，再新增依赖和运行时，不把旧内核换一个名字重写。

## 历史设计资料

以下文档记录已经完成或被取代的阶段，不再直接驱动编码：

- `第一阶段.md`
- `FinanceClaw-第二阶段说明书.md`
- `FinanceClaw-Stage3A-Provider-Fabric-实施说明书.md`（Provider Fabric 保留）
- `FinanceClaw-Stage3B-Routing-Planning-实施说明书.md`（Routing/Planning 已被取代）
- `FinanceClaw-Stage3C-Agentic-Exploration-实施说明书.md`（自研 Agent/DAG runtime 已被取代）
- `FinanceClaw-第三阶段说明书.md`（仅作历史上位设计）

## 建议阅读顺序

```text
LemonClaw 对齐分析
  → 顶层 Agent / Workflow ADR
  → LangChain ADR
  → LangGraph ADR
  → Stage 3 ADR 状态摘要
  → Context / Memory 的历史实施说明（需要细化领域语义时）
```
