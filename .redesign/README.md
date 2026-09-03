# FinanceClaw Redesign

状态：已确认，作为下一轮实现与删除工作的唯一目标架构基线。

更新时间：2026-09-03

## 目的

本目录把 FinanceClaw 从“自研通用 Agent Harness”收敛为“成熟 Agent 运行时之上的金融领域核心”。核心原则是：

- LangChain 负责模型、Tool、Agent Loop、Middleware、retry/fallback 与 structured output；
- LangGraph Agent Server 负责 Graph、Thread、Run、Checkpoint、Store、队列、Streaming、interrupt/resume；
- LangSmith 负责 Agent/Workflow/Model/Tool 调用链观测、调试和评测；
- FinanceClaw 只保留金融场景真正需要的会话、记忆、上下文选择、工具治理、安全、审批和审计语义；
- 不再为成熟框架已经覆盖的能力建立第二套 Contract、SPI、Registry 或 Runtime。

`.design/` 中的旧文档继续作为历史演进记录；当其内容与本目录冲突时，以 `.redesign/` 为准。

## 已冻结的关键决议

1. 运行时统一使用稳定的 CPython `>=3.13,<3.14`。
2. 所有产品会话消息统一进入默认顶层 Agent，由其在 ReAct 循环中回答、调用 Tool、调用 Workflow 或委派领域 Agent；对外 API 不接受 Agent/Tool/Workflow Target。
3. 不再要求 LLM 构造复杂 `PlanDraft`；确定性业务流程发布为版本化 LangGraph Workflow。
4. Tool 统一使用 LangChain `BaseTool`；Capability、Provider Registry、通用 Selection 和旧 Invoker 退出。
5. LangChain 没有通用 Tool 业务 RBAC/ABAC；FinanceClaw 只保留薄 `ToolGovernance` 与确定性 Policy 函数，并通过 Middleware/HITL 落地。
6. 原始多轮会话永久保存且无自动 TTL；Prompt 使用最近窗口、分段/分层摘要和相关历史召回控制长度。
7. LangGraph Checkpointer 管短期状态；LangGraph Store 管长期 Agent Memory 存储；金融实时事实必须通过领域 Tool/Service 获取。
8. 调试环境输出完整 Prompt、Tool Schema、模型输入输出和 Tool I/O；生产按数据分类脱敏。
9. 调用链观测以 LangSmith 为主；OpenTelemetry 只补充 HTTP、数据库、队列等基础设施观测；金融 Audit 独立永久保存。
10. LangGraph Agent Server 是内部执行平面，FinanceClaw API/BFF 是唯一产品与业务安全入口。

## 文档导航

- [最终架构设计](./00-最终架构设计.md)
- [架构决议汇总](./01-架构决议汇总.md)
- [目标模块与依赖设计](./02-目标模块与依赖设计.md)
- [数据模型与持久化设计](./03-数据模型与持久化设计.md)
- [安全、观测与评测设计](./04-安全观测与评测设计.md)
- [顶层 Agent 与对外接口修订](./05-顶层Agent与对外接口修订.md)

实施阶段：

- [Stage 0：Framework Spike](./stages/Stage-0-Framework-Spike-实施说明.md)
  - [Stage 0 验证记录](./stages/Stage-0-验证记录.md)
- [Stage 1：Execution Spine](./stages/Stage-1-Execution-Spine-实施说明.md)
  - [Stage 1 验证记录](./stages/Stage-1-验证记录.md)
- [Stage 2：Conversation Context](./stages/Stage-2-Conversation-Context-实施说明.md)
  - [Stage 2 验证记录](./stages/Stage-2-验证记录.md)
- [Stage 3：Long-term Memory](./stages/Stage-3-Long-term-Memory-实施说明.md)
- [Stage 4：Published Workflows](./stages/Stage-4-Published-Workflows-实施说明.md)
- [Stage 5：Production Hardening](./stages/Stage-5-Production-Hardening-实施说明.md)

迁移材料：

- [旧模块删除映射](./migration/旧模块删除映射.md)
- [依赖与迁移顺序](./migration/依赖与迁移顺序.md)

## 使用规则

- 每个 Stage 必须先满足其验收条件，再删除对应旧模块。
- 新实现不得引用将被删除的 Capability/Provider/Plan Runtime。
- 框架原生对象只在 API、持久化或审计边界转换为 FinanceClaw DTO。
- 新增自研抽象前必须先确认 LangChain、LangGraph、LangSmith、MCP 或成熟基础设施没有覆盖。
- 所有架构例外必须以新的 ADR 记录，不能在实现中静默偏离。
