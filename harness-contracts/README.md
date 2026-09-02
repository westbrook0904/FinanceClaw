# harness-contracts

跨 FinanceClaw 核心模块与业务插件共享的稳定、不可变 Pydantic 协议：

- Request/Input/Target/Options；
- Agent/Tool Capability 与 execution profile；
- Provider identity、health、attempt、selection 与 Capability retry；
- Invocation identity、tenant、deadline、trace；
- Context snapshot/projection/use record 与有界 Observation；
- Memory query/record/write proposal/provenance；
- Approval、Result/Continuation、Error。

本包不再定义 ExecutionMode、RouteDecision、PlanDraft、ExecutionPlan、DAG 状态、模型厂商协议或
LangGraph checkpoint schema。框架原生类型停留在适配层内，只有确实需要跨 FinanceClaw 边界
稳定传输的领域语义才进入本包。
