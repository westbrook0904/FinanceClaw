# FinanceClaw Redesign

状态：已确认的架构基线继续有效；新增阶段按各文档状态评审，Proposed 设计不自动成为冻结决议。

更新时间：2026-09-09

Stage-8 前置分包已完成：`bff`、`coordination`、`agent_server`，同库与共享模块边界见
[包结构设计](../docs/architecture/package-layout.md)。Stage-8 独立协调服务已实现；后续方向按
[Stage 8 Hotfix](./stages/stage-8-hotfix-实施方案.md)调整为 BFF 运行控制与顶层内部子图，HF-0 已完成，生产路径待迁移。

## 目的

本目录把 FinanceClaw 从“自研通用 Agent Harness”收敛为“成熟 Agent 运行时之上的金融领域核心”。核心原则是：

- LangChain 负责模型、Tool、Agent Loop、Middleware、retry/fallback 与 structured output；
- LangGraph Agent Server 负责 Graph、Thread、Run、Checkpoint、Store、队列、Streaming、interrupt/resume；
- LangSmith 负责 Agent/Workflow/Model/Tool 调用链观测、调试和评测；
- FinanceClaw 只保留金融场景真正需要的会话、记忆、上下文选择、工具治理、安全、审批和审计语义；
- 不再为成熟框架已经覆盖的能力建立第二套 Contract、SPI、Registry 或 Runtime。

旧 `.design/` 文档已经停止驱动实现并从工作树移除；需要追溯时通过 Git 历史查看，当前实现只以
`.redesign/` 为架构基线。

## 已冻结的关键决议

1. 运行时统一使用稳定的 CPython `>=3.13,<3.14`。
2. 所有产品会话消息统一进入默认顶层 Agent，由其在 ReAct 循环中回答、调用 Tool，并通过 Tool 调用 Workflow／领域 Agent 子图；对外 API 不接受 Agent/Tool/Workflow Target。
3. 不再要求 LLM 构造复杂 `PlanDraft`；确定性业务流程发布为版本化 LangGraph Workflow。
4. Tool 统一使用 LangChain `BaseTool`；Capability、Provider Registry、通用 Selection 和旧 Invoker 退出。
5. LangChain 没有通用 Tool 业务 RBAC/ABAC；FinanceClaw 只保留薄 `ToolGovernance` 与确定性 Policy 函数，并通过 Middleware/HITL 落地。
6. 原始多轮会话永久保存且无自动 TTL；Prompt 使用最近窗口、分段/分层摘要和相关历史召回控制长度。
7. LangGraph Checkpointer 管短期状态；LangGraph Store 管长期 Agent Memory 存储；金融实时事实必须通过领域 Tool/Service 获取。
8. 调试环境输出完整 Prompt、Tool Schema、模型输入输出和 Tool I/O；生产按数据分类脱敏。
9. 调用链观测以 LangSmith 为主；OpenTelemetry 只补充 HTTP、数据库、队列等基础设施观测；金融 Audit 独立永久保存。
10. LangGraph Agent Server 是内部执行平面，FinanceClaw API/BFF 是唯一产品与业务安全入口。

## Stage 8 方向调整

2026-09-09 用户要求取消跨顶层 ReAct 的委托，把 start／resume 等运行控制归还 BFF。
所有 subagent／workflow 改为顶层 Agent 调用的 Tool，实际在 Agent Server 内调用子图。
建议保留 Webhook 接收能力并合入 BFF，以回调加后台核对保证断连后结果写入 Journal；
不再保留独立 Coordinator 的新运行编排职责，继续共用 `financeclaw_app`，不引入 Temporal。

当前代码仍是已交付的 Stage 8A／8B／8C；hotfix 方案和分阶段验收见
[实施方案](./stages/stage-8-hotfix-实施方案.md)及
[RD-033](./01-架构决议汇总.md#rd-033顶层-react-内部子图与-bff-运行控制)。
旧 Stage 8 方案与验证记录保留为历史资料，其中跨 Run 委托和 Coordinator 执行所有权不再作为新实施方向。

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
- [Stage 6 Fix：委派可靠性、用户交互与批量工具调用优化方案（Proposed）](./stages/stage-6-fix.md)
- [Stage 7：Ziwei Domain Agent 设计（候选实现中）](./stages/Stage-7-Ziwei-Domain-Agent-设计说明.md)
  - [Stage 7 设计审视与待确认决议](./stages/Stage-7-设计审视与待确认决议.md)
  - [Stage 7 实施与验证记录](./stages/Stage-7-实施与验证.md)
  - [Stage 7 文本解读热修复：移除解读 JSON 与逐条引用硬约束](./stages/Stage-7-文本解读热修复-实施与验证.md)
- [Stage 8 Hotfix：BFF 运行控制与顶层 ReAct 内的子图调用（HF-0 完成，HF-1～HF-3 待实施）](./stages/stage-8-hotfix-实施方案.md)
  - [HF-0 实施与验证：原生子图调用、顶层恢复与发布预留](./stages/stage-8-hotfix-HF-0-实施与验证.md)
- [Stage 8 原方案：Coordinator Service、Webhook 接入与显式委派协议（历史方案，方向已被 hotfix 替代）](./stages/Stage-8-Background-Run-Coordination-实施方案.md)
- [Stage 8 实施与验证：协议、事务与基础协调证据](./stages/Stage-8-实施与验证.md)
- [Stage 8A 实施与验证：正式 Coordinator 与 LangGraph 闭环](./stages/Stage-8A-实施与验证.md)
- [Stage 8B 实施与验证：持久通知、独立飞书发送器与 SSE 恢复](./stages/Stage-8B-实施与验证.md)
- [Stage 8C 实施与验证：旧根接管、部署门闩与多进程容量](./stages/Stage-8C-实施与验证.md)

迁移材料：

- [旧模块删除映射](./migration/旧模块删除映射.md)
- [依赖与迁移顺序](./migration/依赖与迁移顺序.md)

## 使用规则

- 每个 Stage 必须先满足其验收条件，再删除对应旧模块。
- 新实现不得引用将被删除的 Capability/Provider/Plan Runtime。
- 框架原生对象只在 API、持久化或审计边界转换为 FinanceClaw DTO。
- 新增自研抽象前必须先确认 LangChain、LangGraph、LangSmith、MCP 或成熟基础设施没有覆盖。
- 所有架构例外必须以新的 ADR 记录，不能在实现中静默偏离。
- Stage 7 是现有金融核心上的受限传统文化咨询扩展提案；规则、引擎和隐私例外须经评审，
  不以文档新增替代批准，也不将命理解释用作金融事实或决策依据。
