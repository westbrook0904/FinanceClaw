# FinanceClaw 设计文档索引

> **当前实施原则**：先完成并真实使用单 Agent 基础闭环，再讨论 HYBRID、PlanPatch、
> 高阶资源预算和复杂分布式恢复。

## 当前有效基线

| 文档 | 定位 | 当前状态 |
|---|---|---|
| `FinanceClaw-Agent-Foundation-一期路线图.md` | 一期范围、优先级与验收入口 | **当前路线图** |
| `FinanceClaw-Agent-Foundation-一期实施说明书.md` | Context、Memory、最小 Explore 的唯一实施契约 | **当前实施基线** |
| `FinanceClaw-第三阶段说明书.md` | 第三阶段上位架构与阶段边界 | **按一期路线图修订后有效** |
| `FinanceClaw-第三阶段待讨论ADR.md` | 已决议与延期项摘要 | **当前 ADR 摘要** |

当前进度：Foundation 0 前置收口、Foundation 1 Routing correctness 与 Foundation 2 Context
Engineering 已完成；下一推荐步骤为 Foundation 3 Memory。

## 已完成阶段的历史实施基线

以下文档记录当时的实施边界，不作为当前优先级来源：

- `第一阶段.md`
- `FinanceClaw-第二阶段说明书.md`
- `FinanceClaw-Stage3A-Provider-Fabric-实施说明书.md`
- `FinanceClaw-Stage3B-Routing-Planning-实施说明书.md`

它们包含的“未来阶段”描述若与一期路线图不同，以一期路线图为准。

## 目标架构与设计储备

`FinanceClaw-Stage3C-Agentic-Exploration-实施说明书.md` 是旧版完整 Agentic Orchestration 草案。
它保留 HYBRID、PlanPatch、Approval/Async 和复杂恢复细节，整体不作为当前编码任务来源。

`Harness-Agent_通用可插拔智能体平台架构设计_修订版.md` 描述长期目标架构，不等于当前
实施清单。其中 HYBRID、PlanPatch、多 Agent、自动 Workflow、高阶预算等内容均属于设计储备。

`Fo-Finance-Agent 系统架构图.png` 同样是旧版目标态示意图，其中中心 Agent、多 Agent、
长期记忆和成本/Token 观测不能被解释为一期已经实现或必须同时交付的组件。

## 阅读顺序

```text
一期路线图
  ↓
第三阶段说明书
  ↓
Agent Foundation 一期实施说明书
  ↓
需要追溯时再阅读历史实施基线或高阶设计储备
```

不得仅因为某个 Contract、ExecutionMode 或章节已经存在，就把对应能力视为当前应实现范围。
