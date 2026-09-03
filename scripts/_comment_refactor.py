"""Generate an apply_patch payload for the one-off FinanceClaw comment rewrite.

This helper deliberately emits a patch instead of modifying source files itself.  It is
temporary implementation machinery and is removed after the documentation pass.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import re
import textwrap
from pathlib import Path


MODULE_DOCS = {
    "application": "应用层用例编排：连接接口层、领域模块与 Agent Server 端口。",
    "evaluation": "回归评测数据、结果校验与发布门禁。",
    "infrastructure": "数据库、模型供应商、安全策略和可观测性等基础设施适配。",
    "interfaces": "对外接口适配，将传输协议转换为应用层请求与响应。",
    "kernel": "跨层共享且不依赖具体实现的运行上下文、目标与响应契约。",
    "modules": "按业务能力拆分的领域模型、仓储与领域服务。",
    "operations": "供部署验证、数据播种和故障排查使用的运维入口。",
    "orchestration": "Agent、工具和 LangGraph 工作流的运行时编排。",
}

FILE_DOCS = {
    "financeclaw/bootstrap.py": "组装 FinanceClaw 的配置、基础设施、领域服务与运行时组件。",
    "financeclaw/application/conversation_service.py": "围绕 Agent Server 运行编排持久化的多轮会话。",
    "financeclaw/application/delegation_service.py": "编排父运行向工作流或专业 Agent 的可恢复委派。",
    "financeclaw/application/run_service.py": "提供无会话运行的启动、查询、审批恢复与事件流接口。",
    "financeclaw/application/target_resolver.py": "把外部运行目标解析为固定版本的 Agent Server 调用参数。",
    "financeclaw/application/workflow_service.py": "管理持久化工作流运行及其中断审批生命周期。",
    "financeclaw/application/ports/agent_server.py": "定义应用层访问 Agent Server 所需的最小异步端口。",
    "financeclaw/infrastructure/database.py": "管理 SQLAlchemy 引擎、事务会话和数据库连通性。",
    "financeclaw/infrastructure/orm.py": "提供领域表模型共享的 SQLAlchemy 声明基类与 UTC 时间工厂。",
    "financeclaw/infrastructure/settings.py": "集中声明环境变量配置，并校验生产环境安全约束。",
    "financeclaw/infrastructure/clients/agent_server.py": "通过 LangGraph SDK 实现 Agent Server 应用端口。",
    "financeclaw/infrastructure/llm/factory.py": "依据受治理的模型配置创建主模型与回退模型实例。",
    "financeclaw/infrastructure/llm/profiles.py": "定义可版本化的模型配置及其只读目录。",
    "financeclaw/infrastructure/security/egress.py": "校验外部 URL 是否满足协议、主机白名单和私网限制。",
    "financeclaw/infrastructure/observability/logging.py": "提供敏感字段脱敏与结构化 JSON 日志配置。",
    "financeclaw/infrastructure/observability/telemetry.py": "配置 OpenTelemetry，并采集 HTTP 与数据库运行指标。",
    "financeclaw/infrastructure/observability/langsmith.py": "把 LangSmith 跟踪选项写入 SDK 使用的进程环境。",
    "financeclaw/interfaces/http/app.py": "创建 FastAPI 路由，并将鉴权后的 HTTP 请求交给应用服务。",
    "financeclaw/interfaces/http/auth.py": "解析 Bearer 凭证并构造带租户、主体和权限域的身份。",
    "financeclaw/interfaces/http/errors.py": "把领域及应用异常映射为稳定的 HTTP 错误响应。",
    "financeclaw/interfaces/http/streaming.py": "把内部流事件编码为 Server-Sent Events 数据帧。",
    "financeclaw/modules/artifacts/models.py": "定义大结果外置后使用的不可变制品引用。",
    "financeclaw/modules/artifacts/repository.py": "持久化制品元数据并提供按内容哈希去重查询。",
    "financeclaw/modules/artifacts/service.py": "按大小阈值把工具结果内联返回或写入制品存储。",
    "financeclaw/modules/artifacts/storage.py": "实现本地文件与 S3 兼容对象存储的制品读写边界。",
    "financeclaw/modules/audit/models.py": "定义授权、执行与记忆操作使用的不可变审计事件。",
    "financeclaw/modules/audit/repository.py": "提供内存及 SQLAlchemy 审计事件仓储。",
    "financeclaw/modules/audit/tables.py": "声明审计事件的 SQLAlchemy 持久化映射。",
    "financeclaw/modules/conversation/models.py": "定义会话、消息、摘要和模型上下文清单领域记录。",
    "financeclaw/modules/conversation/context.py": "在模型输入预算内选择消息、摘要、记忆和工具结果。",
    "financeclaw/modules/conversation/repository.py": "维护会话 Journal、消息序列、运行状态和上下文清单。",
    "financeclaw/modules/conversation/summaries.py": "生成分层会话摘要并维护摘要替代关系。",
    "financeclaw/modules/conversation/tables.py": "声明会话 Journal 相关 SQLAlchemy 表映射。",
    "financeclaw/modules/delegation/models.py": "定义子 Agent 或工作流委派的请求、状态和持久化记录。",
    "financeclaw/modules/delegation/naming.py": "为委派的子线程生成可复现且合法的稳定标识。",
    "financeclaw/modules/delegation/repository.py": "持久化委派记录，并以乐观并发控制推进状态。",
    "financeclaw/modules/delegation/tables.py": "声明委派记录的 SQLAlchemy 持久化映射。",
    "financeclaw/modules/memory/models.py": "定义长期记忆、候选、检索结果和审计证据模型。",
    "financeclaw/modules/memory/policy.py": "根据记忆类型、敏感度和来源决定确认与写入策略。",
    "financeclaw/modules/memory/service.py": "管理长期记忆的提议、确认、检索、撤销与删除生命周期。",
    "financeclaw/modules/outbox/models.py": "定义事务 Outbox 事件及其投递状态。",
    "financeclaw/modules/outbox/publisher.py": "批量发布待处理 Outbox 事件并记录成功或失败结果。",
    "financeclaw/modules/outbox/repository.py": "在数据库事务中写入、领取和更新 Outbox 事件。",
    "financeclaw/modules/outbox/tables.py": "声明 Outbox 事件的 SQLAlchemy 持久化映射。",
    "financeclaw/modules/workflows/catalog.py": "登记并解析不可变版本的确定性工作流定义。",
    "financeclaw/modules/workflows/models.py": "定义工作流输入目标、运行状态、审批和版本化定义。",
    "financeclaw/modules/workflows/repository.py": "持久化工作流运行、审批快照及乐观锁版本。",
    "financeclaw/modules/workflows/tables.py": "声明工作流运行与审批的 SQLAlchemy 表映射。",
    "financeclaw/orchestration/agents/artifact_middleware.py": "在工具返回模型前自动外置过大的结果载荷。",
    "financeclaw/orchestration/agents/context_middleware.py": "在每次模型调用前装配并记录可复现的会话上下文。",
    "financeclaw/orchestration/agents/directive_middleware.py": "识别显式调用指令，限制该轮模型可见的工具集合。",
    "financeclaw/orchestration/agents/directives.py": "解析显式 Agent、工作流或工具调用指令并校验工具参数槽位。",
    "financeclaw/orchestration/agents/factory.py": "依据 Agent 配置组装模型、工具与治理中间件。",
    "financeclaw/orchestration/agents/memory_middleware.py": "检索与当前问题相关的长期记忆并注入模型提示。",
    "financeclaw/orchestration/agents/middleware.py": "实现上下文跟踪、工具治理和受控完整输入输出日志。",
    "financeclaw/orchestration/agents/offline.py": "提供无需外部模型服务即可验证编排路径的确定性聊天模型。",
    "financeclaw/orchestration/agents/profiles.py": "定义版本化 Agent 配置及其只读目录。",
    "financeclaw/orchestration/graphs/direct_tool.py": "构建带策略判断、审批中断、重试和制品投影的直接工具图。",
    "financeclaw/orchestration/graphs/finance_agent.py": "向 LangGraph Server 暴露配置驱动的顶层 Agent 与直接工具图。",
    "financeclaw/orchestration/graphs/server_graphs.py": "声明 LangGraph Server 可发现的图工厂导出。",
    "financeclaw/orchestration/graphs/workflows/portfolio_review_v1.py": "实现可审计、可审批且输出固定结构的投资组合复核工作流。",
    "financeclaw/orchestration/tools/catalog.py": "提供按语义版本登记和解析的受治理工具目录。",
    "financeclaw/orchestration/tools/delegation.py": "把 Agent 与工作流委派目标包装成可治理工具。",
    "financeclaw/orchestration/tools/governance.py": "定义工具副作用、风险、审批、出站和审计策略元数据。",
    "financeclaw/orchestration/tools/local.py": "提供开发、测试和离线运行所需的本地金融工具。",
    "financeclaw/orchestration/tools/mcp.py": "把远端 MCP 行情能力适配为受治理的 LangChain 工具。",
    "financeclaw/orchestration/tools/mcp_server.py": "暴露 FinanceClaw 示例行情 MCP 服务端工具。",
    "financeclaw/orchestration/tools/memory.py": "把长期记忆生命周期操作暴露为受治理工具。",
    "financeclaw/orchestration/tools/policy.py": "根据执行上下文和治理元数据作出工具授权、审批与重试判断。",
}

CLASS_ROLES = {
    "ApprovalExpired": "表示审批窗口已过期，原检查点仍保留但不得继续恢复。",
    "ConversationService": "协调根 Agent 会话的 Journal、Agent Server 运行、审批和子委派。",
    "DelegationService": "协调父运行与子 Agent/工作流之间的创建、轮询、恢复和结果交付。",
    "RunService": "管理不依赖持久化会话的短生命周期 Agent 或工具运行。",
    "TargetResolver": "把外部目标请求解析为固定版本、可直接提交给 Agent Server 的目标。",
    "WorkflowService": "管理确定性工作流从接收、执行、中断审批到终态落库的全过程。",
    "AgentServerClient": "约束应用层对 Agent Server 的线程、运行、恢复和流式访问能力。",
    "LangGraphAgentServerClient": "使用 LangGraph SDK 调用远端 Agent Server，并隐藏传输细节。",
    "ApplicationDatabase": "持有数据库引擎与会话工厂，统一事务和健康检查生命周期。",
    "ModelFactory": "按照模型配置创建供应商客户端，并构造配置声明的回退链。",
    "ModelProfileCatalog": "保存不可变模型配置，以“配置 ID + 版本”稳定解析。",
    "AgentProfileCatalog": "保存不可变 Agent 配置，并支持解析指定版本或最新版本。",
    "FinanceClawSettings": "汇总环境变量配置，并阻止不安全的生产环境组合启动。",
    "FinanceClawComponents": "保存一次启动中组装完成的共享组件，供 HTTP 与图工厂复用。",
    "EgressPolicy": "在创建网络客户端前验证 URL 的协议、主机白名单和私网属性。",
    "JsonLogFormatter": "把日志记录格式化为单行 JSON，同时对附加字段做递归脱敏。",
    "TelemetryRuntime": "持有可选的追踪与指标 Provider，并提供统一关闭入口。",
    "_RequestObservabilityMiddleware": "围绕每个 ASGI HTTP 请求采集耗时、状态码和异常追踪。",
    "ConversationContextBuilder": "在输入预算内组合近期消息、摘要、长期记忆和历史证据。",
    "SummaryService": "把达到阈值的消息或低层摘要压缩为可复现的分层摘要。",
    "LongTermMemoryService": "实施长期记忆的证据校验、确认策略、版本更新与审计。",
    "MemoryPolicy": "把记忆候选分类为自动提交、需确认或拒绝。",
    "OutboxPublisher": "领取待投递事件，调用发布函数，并把投递结果可靠回写。",
    "WorkflowCatalog": "登记工作流版本并解析显式版本或某工作流的最新版本。",
    "ToolCatalog": "登记受治理工具版本并提供精确或最新版本查询。",
    "ToolPolicy": "结合调用者权限与工具元数据返回允许、拒绝或需审批决策。",
    "AgentFactory": "将 Agent 配置解析为模型、工具、限额及中间件组成的运行实例。",
    "ToolGovernanceMiddleware": "在工具可见性与执行两个阶段强制实施授权、审批和审计。",
    "ConversationContextMiddleware": "在模型调用前替换系统上下文，并持久化本次选择清单。",
    "MemoryRecallMiddleware": "在模型调用前检索相关记忆，并按独立预算裁剪后注入。",
    "InvocationDirectiveMiddleware": "将显式调用请求约束为单个目标工具，减少模型路由歧义。",
    "ToolResultArtifactMiddleware": "将超出内联阈值的工具结果替换为持久化制品引用。",
    "FullIODebugMiddleware": "仅在显式启用时记录脱敏后的模型与工具完整输入输出。",
    "OfflineFinanceModel": "以确定性规则模拟模型工具调用，用于离线测试和冒烟验证。",
    "RegressionGate": "核对评测用例与结果，阻止缺失、失败或低于阈值的发布。",
}

CLASS_SCENARIOS = {
    "Repository": "用于领域服务需要持久化状态，同时不应感知 SQL 细节的场景。",
    "Service": "用于应用用例需要跨仓储、外部端口或领域策略协调一致结果的场景。",
    "Catalog": "用于运行时必须按显式版本复现配置，或选择最新兼容版本的场景。",
    "Middleware": "用于 Agent 模型或工具调用进入下一处理器前后的横切治理场景。",
    "Tool": "用于把该能力纳入 LangChain/LangGraph 工具调用与统一治理链的场景。",
    "Input": "用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。",
    "Request": "用于接口层接收并校验调用方输入，再交给应用服务处理。",
    "Output": "用于把内部执行结果投影为稳定、可序列化的边界响应。",
    "Response": "用于接口层向调用方返回稳定结构，避免泄露内部实现对象。",
    "Record": "用于跨步骤保存不可变事实，并支持持久化或审计重放。",
    "State": "用于 LangGraph 节点之间共享逐步填充的运行状态。",
    "Table": "用于把领域记录映射到关系数据库，不承载业务决策。",
    "Profile": "用于以版本化配置固定运行行为，确保审计与结果可复现。",
    "Policy": "用于在执行副作用前作出确定性治理决策。",
}

ENUM_ROLES = {
    "DataClassification": "运行上下文或供应商配置允许处理的数据敏感等级。",
    "ConversationStatus": "会话是否仍允许继续追加轮次。",
    "TurnStatus": "会话轮次从接收到完成或失败的生命周期状态。",
    "MessageRole": "消息在对话中的发送方角色。",
    "SummaryStatus": "摘要是否仍是当前有效版本。",
    "DelegationKind": "委派目标属于工作流还是专业 Agent。",
    "DelegationStatus": "委派从创建到交付或失败的生命周期状态。",
    "MemoryType": "可长期保存的信息语义类别。",
    "MemoryStatus": "长期记忆当前是否可检索或已撤销。",
    "MemoryEventType": "长期记忆生命周期中的审计事件类别。",
    "OutboxStatus": "Outbox 事件等待、完成或永久失败的投递状态。",
    "WorkflowRunStatus": "工作流运行从接收到终态的生命周期状态。",
    "ApprovalStatus": "工作流审批请求的待决或终态状态。",
    "SideEffect": "工具调用对外部状态产生的副作用类型。",
    "Idempotency": "工具是否支持重试以及是否强制提供幂等键。",
    "RiskLevel": "工具调用的业务风险等级。",
    "ApprovalMode": "工具在执行前是否必须取得人工批准。",
    "Egress": "工具调用是否访问内部或外部网络。",
    "Sensitivity": "工具可处理数据的最高敏感级别。",
    "RetryProfile": "工具失败后可采用的重试策略。",
    "AuditLevel": "工具调用需记录的审计详细程度。",
    "ToolDecisionType": "策略引擎对一次工具调用作出的决策类型。",
    "InvocationKind": "显式调用指令指向工具、工作流或 Agent。",
    "Environment": "应用部署环境；生产环境会触发更严格的配置校验。",
    "ArtifactBackend": "制品二进制内容使用的存储后端。",
}

FIELD_DOCS = {
    "model_config": "Pydantic 校验策略，禁止未知字段并在需要时冻结实例。",
    "conversation_id": "会话稳定标识，用于关联消息、轮次、摘要和上下文清单。",
    "turn_id": "会话轮次标识，用于把一次用户输入与其运行结果关联。",
    "run_id": "应用侧运行标识，用于跨服务查询、追踪和幂等关联。",
    "server_run_id": "Agent Server 侧运行标识；尚未提交远端运行时为空。",
    "thread_id": "Agent Server 线程标识，用于保存运行检查点与消息状态。",
    "agent_thread_id": "根 Agent 使用的服务端线程标识。",
    "tenant_id": "租户隔离键，所有读取和写入都必须以此限定边界。",
    "subject_id": "已认证主体标识，用于所有权校验和审计归因。",
    "agent_id": "Agent 配置的稳定标识。",
    "assistant_id": "提交 Agent Server 时使用的助手或图标识。",
    "workflow_id": "工作流的稳定标识。",
    "tool_id": "工具的稳定标识。",
    "profile_id": "版本化配置的稳定标识。",
    "version": "语义版本，用于固定运行行为并支持审计复现。",
    "target_version": "运行实际绑定的目标版本，防止后续配置变化影响重放。",
    "agent_profile_version": "本次运行固定使用的 Agent 配置版本。",
    "model_profile_version": "本次模型调用固定使用的模型配置版本。",
    "prompt_template_version": "构造模型提示时使用的模板版本。",
    "template_version": "生成该内容时使用的模板版本。",
    "schema_version": "记录结构版本，用于兼容演进和历史数据解析。",
    "status": "当前生命周期状态，决定记录允许的后续操作。",
    "created_at": "记录创建时间，统一按 UTC 解释。",
    "updated_at": "最近一次状态或内容变更时间，统一按 UTC 解释。",
    "completed_at": "进入成功或失败终态的时间；未结束时为空。",
    "expires_at": "记录或审批失效时间；为空表示不按时间自动失效。",
    "content": "经过边界校验后保存或传递的正文内容。",
    "content_hash": "正文的 SHA-256，用于完整性校验、去重与审计。",
    "arguments": "传给目标工具或工作流的已解析参数。",
    "arguments_hash": "规范化参数的 SHA-256，用于审批绑定和篡改检测。",
    "input": "提交给运行目标的结构化输入。",
    "output": "运行完成后的结构化输出；尚未完成时为空。",
    "error": "失败原因的稳定文本；成功或未结束时为空。",
    "scopes": "调用主体拥有的权限域集合。",
    "required_scopes": "执行目标必须具备的权限域集合。",
    "description": "供调用者、模型或运维人员理解用途的可读说明。",
    "name": "在外部接口或工具注册表中暴露的稳定名称。",
    "args_schema": "工具入参使用的 Pydantic 校验模型类型。",
    "kind": "记录或目标的语义类别。",
    "risk_level": "调用风险等级，用于决定审批与审计强度。",
    "side_effect": "调用对外部状态的影响类别。",
    "approval": "执行前采用的人工审批策略。",
    "egress": "调用所需的网络出站范围。",
    "sensitivity": "允许处理的数据敏感级别。",
    "audit_level": "授权与执行过程要求的审计粒度。",
    "retry_profile": "失败后允许采用的重试策略。",
    "idempotency": "调用的幂等能力及幂等键要求。",
    "direct_invocation": "是否允许调用方绕过 Agent 规划直接执行该工具。",
    "allowed_data_classes": "该配置允许发送或处理的数据分类集合。",
    "allowed_regions": "模型请求允许落地或处理数据的区域集合。",
    "tenant_allowlist": "可使用该工具的租户白名单；为空表示不额外限制。",
    "effect": "策略决策结果：允许、拒绝或要求审批。",
    "reason": "产生当前决策、遗漏或状态的可读原因。",
    "policy_version": "作出决策时使用的策略版本。",
    "fingerprint": "请求规范化后的指纹，用于识别幂等重放与冲突。",
    "idempotency_key": "调用方提供的幂等键，用于安全重试。",
    "client_idempotency_key": "客户端幂等键，在同一资源范围内唯一。",
    "request_hash": "请求规范化后的哈希，用于检测幂等键复用冲突。",
    "context": "本次运行的租户、主体、权限与关联标识上下文。",
    "metadata": "随运行或记录保存的非业务控制信息。",
    "role": "消息发送方角色。",
    "sequence": "消息在会话内从 1 开始的稳定顺序号。",
    "parent_message_id": "被该消息直接响应的父消息标识；无父消息时为空。",
    "visible": "该消息是否应暴露给后续模型上下文。",
    "summary_id": "摘要稳定标识。",
    "level": "摘要层级；0 表示直接由原始消息生成。",
    "start_sequence": "摘要覆盖的第一条消息序号。",
    "end_sequence": "摘要覆盖的最后一条消息序号。",
    "source_message_ids": "生成摘要时使用的原始消息标识，保留证据链。",
    "source_summary_ids": "生成高层摘要时使用的低层摘要标识。",
    "summary_content": "提供给模型的压缩会话内容。",
    "topics": "摘要提取出的主题标签。",
    "entities": "摘要提取出的实体名称。",
    "decisions": "摘要提取出的已确认决策。",
    "open_items": "摘要提取出的未完成事项。",
    "superseded_by": "替代当前记录的新版本标识；仍有效时为空。",
    "memory_id": "长期记忆稳定标识。",
    "memory_ids": "本次上下文实际注入的长期记忆标识，顺序与引用一致。",
    "memory_refs": "带版本和注入原因的长期记忆引用。",
    "memory_type": "长期记忆的语义类别。",
    "injection_reason": "该记忆与当前问题相关并被注入的原因。",
    "manifest_id": "模型上下文清单标识，用于复现单次模型调用输入。",
    "model_call_id": "一次具体模型调用的关联标识。",
    "recent_message_start": "本次选择的近期消息起始序号。",
    "recent_message_end": "本次选择的近期消息结束序号。",
    "summary_ids": "本次上下文使用的摘要标识。",
    "historical_message_ids": "因相关性被补充选择的较早消息标识。",
    "tool_result_refs": "上下文引用的外置工具结果标识。",
    "exposed_tools": "本次模型调用可见的工具名称。",
    "input_token_count": "最终选择内容的估算输入 token 数。",
    "available_input_tokens": "扣除输出、系统策略和安全余量后的输入预算。",
    "omissions": "因预算或相关性未纳入上下文的条目及原因。",
    "context_hash": "最终上下文选择的稳定哈希，用于审计和复现。",
    "debug_payload": "仅调试模式保存的上下文明细；正常模式保持为空。",
    "event_id": "审计或 Outbox 事件的稳定标识。",
    "event_type": "事件的语义类型，供消费者选择处理逻辑。",
    "payload": "事件携带的结构化业务数据。",
    "payload_hash": "事件载荷的稳定哈希，用于完整性核对。",
    "attempts": "已经尝试投递或执行的次数。",
    "next_attempt_at": "失败后允许再次尝试的最早时间。",
    "last_error": "最近一次失败原因，尚未失败时为空。",
    "artifact_id": "制品稳定标识。",
    "storage_uri": "制品内容的存储位置，不包含访问凭证。",
    "media_type": "制品内容的 MIME 类型。",
    "size_bytes": "制品序列化后的字节数。",
    "inline": "内容是否直接包含在响应中而非外置存储。",
    "delegation_id": "一次父子运行委派的稳定标识。",
    "parent_run_id": "发起委派的父运行标识。",
    "parent_turn_id": "发起委派的父会话轮次标识。",
    "child_run_id": "实际执行委派任务的子运行标识。",
    "child_thread_id": "子 Agent 保存检查点与消息的线程标识。",
    "handoff_id": "由父运行和工具调用确定生成的幂等委派标识。",
    "handoff_kind": "委派目标种类，决定交给工作流还是 Agent。",
    "target_id": "解析前或解析后的目标稳定标识。",
    "target_type": "目标类别，用于区分 Agent、工具和工作流。",
    "workflow_version": "本次运行固定使用的工作流版本。",
    "definition": "工作流的版本化构建器和运行约束。",
    "approval_id": "审批请求稳定标识。",
    "approved_hash": "审批时确认的参数哈希，防止恢复时参数被替换。",
    "decision": "审批人或策略引擎作出的结构化决定。",
    "revision": "乐观锁版本号，每次成功状态变更后递增。",
    "required": "当前运行是否必须等待人工审批。",
    "provider": "产生行情或模型结果的供应方标识。",
    "as_of": "数据在供应方侧生效或采集的时间。",
    "symbol": "标准化金融标的代码。",
    "quantity": "持仓数量。",
    "cost_basis": "持仓单位成本或约定的成本基础。",
    "portfolio_name": "用户可读的投资组合名称。",
    "positions": "待复核的持仓列表。",
    "max_snapshot_age_hours": "可接受的行情快照最大时效（小时）。",
    "snapshot_as_of": "本次分析所用行情中最早或统一的生效时间。",
    "total_market_value": "按行情计算的组合总市值，使用十进制字符串输出。",
    "largest_position_weight": "最大单一持仓占组合市值的比例。",
    "risk_band": "根据集中度等确定性规则得到的风险分档。",
    "source_refs": "支撑结果的行情来源、时间和版本引用。",
    "artifact": "详细报告的制品引用；未生成时为空。",
    "message": "调用方提交的自然语言消息，是本次规划或会话轮次的主要输入。",
    "messages": "按会话顺序排列的消息集合。",
    "target": "调用方指定的运行目标；为空时使用平台默认根 Agent。",
    "target_kind": "实际运行目标类别，用于调用方解释运行语义。",
    "idempotent_replay": "本次结果是否来自相同幂等键和请求内容的安全重放。",
    "content_type": "制品内容的 MIME 类型，供下载方选择解析方式。",
    "data_classification": "本次运行处理的数据分类，用于约束模型和工具选择。",
    "locale": "模型面向用户生成内容时采用的语言与地区标记。",
    "timezone": "解释用户时间和展示时间时采用的 IANA 时区。",
    "code": "稳定错误代码，供客户端分支处理而不依赖提示文本。",
    "details": "可安全返回给调用方的结构化错误详情。",
    "action": "审批点准备执行的动作名称。",
    "resource_type": "被审批、审计或事件关联的资源类别。",
    "requested_action": "需要人工确认的具体操作。",
    "approval_point": "工作流中触发本次人工确认的稳定节点名称。",
    "approval_points": "该工作流定义允许产生中断的节点集合。",
    "approval_outcome": "人工审批结果，用于决定继续、拒绝或按编辑后参数执行。",
    "decided_by": "作出审批决定的主体标识。",
    "decision_reason": "审批人或策略给出的决定理由。",
    "authorization_decision": "执行前记录的策略授权结果。",
    "requires_confirmation": "该候选是否必须经用户确认后才能成为有效长期记忆。",
    "confirmation_reason": "要求确认或允许自动提交的策略理由。",
    "evidence_refs": "支撑该记忆事实的消息或外部证据引用。",
    "provenance": "内容来源、生成方式和版本组成的可审计来源信息。",
    "source_type": "内容来源类别，例如用户陈述或系统推导。",
    "valid_until": "该事实可被使用的截止时间；为空表示没有显式有效期。",
    "namespace": "LangGraph Store 中用于隔离租户、主体和记忆类别的路径。",
    "query": "用于检索或匹配记录的自然语言查询；为空时不做文本过滤。",
    "kinds": "允许返回或操作的记忆类别集合。",
    "limit": "单次操作最多返回的记录数量。",
    "draft": "尚未提交为长期记忆的候选事实。",
    "category": "回归用例所属类别，便于分组统计和门禁定位。",
    "critical": "该用例失败是否必须立即阻止发布。",
    "inputs": "执行评测用例所需的结构化输入。",
    "reference_outputs": "评测时用于比较的预期关键输出。",
    "passed": "该用例是否满足验收条件。",
    "score": "评测得分，通常归一化到 0 至 1。",
    "minimum_score": "非关键用例汇总后允许通过门禁的最低得分。",
    "model": "供应商模型名称。",
    "temperature": "模型采样温度；较低值用于提高可复现性。",
    "fallback_profiles": "主模型失败时按顺序尝试的备用模型配置引用。",
    "fallback_models": "主模型不可用时按顺序尝试的供应商模型名称。",
    "model_profile": "Agent 固定使用的模型配置引用。",
    "system_prompt_template": "定义 Agent 职责、限制和输出要求的系统提示模板。",
    "middleware_profile": "选择 Agent 运行时中间件组合的配置名称。",
    "context_policy": "选择会话上下文截取与摘要策略的配置名称。",
    "memory_policy": "选择长期记忆检索与写入策略的配置名称。",
    "delegatable": "该 Agent 是否允许作为父运行的委派目标。",
    "tool_calling": "探测结果是否证明模型能够产生结构化工具调用。",
    "structured_output": "模型结构化输出能力的探测结果。",
    "governed_tool_executed": "探测中的工具是否确实经过治理链并成功执行。",
    "trace_url": "本次探测对应的可观测性追踪链接；不可用时为空。",
    "root": "本地制品存储的受控根目录。",
    "bucket": "S3 兼容对象存储桶名称。",
    "prefix": "写入对象键时统一添加的目录前缀。",
    "sse_algorithm": "对象存储服务端加密算法。",
    "encryption_metadata": "证明制品静态加密方式的非敏感元数据。",
    "inline_bytes": "结果允许直接返回而不外置存储的最大字节数。",
    "catalog": "用于解析固定版本目标的只读目录。",
    "repository": "负责领域状态读写和事务一致性的仓储。",
    "client": "负责与外部 Agent Server 或供应商通信的端口实现。",
    "service": "执行该适配层所依赖的领域或应用服务。",
    "audit": "记录授权、执行和状态变化的审计仓储。",
    "clock": "可替换时间源，便于统一 UTC 时间并支持确定性测试。",
    "counter": "估算模型输入 token 的计数器。",
    "builder": "按运行配置创建图、上下文或其他复杂对象的构建器。",
    "summarizer": "把消息或低层摘要压缩为结构化摘要内容的实现。",
    "store": "按命名空间保存长期记忆的 LangGraph Store。",
    "sink": "接收已领取 Outbox 事件的发布函数。",
    "engine": "SQLAlchemy 数据库引擎，持有连接池和方言配置。",
    "session_factory": "创建独立 SQLAlchemy 事务会话的工厂。",
    "graph": "已经编译、可由 Agent Server 执行的 LangGraph 图。",
    "tool": "实际执行能力的 LangChain 工具实例。",
    "governance": "与工具版本绑定的静态治理元数据。",
    "runtime": "LangChain 注入的可信工具运行上下文，不由模型生成。",
    "operation": "计算器允许执行的运算名称。",
    "left": "二元运算左操作数。",
    "right": "二元运算右操作数。",
    "note": "写入观察列表时附带的用户备注。",
    "normalized_arguments": "经过入参模型校验和规范化后的工具参数。",
    "normalized_input": "经过工作流输入模型校验后的规范化数据。",
    "snapshots": "按持仓顺序加载并通过来源校验的行情快照。",
    "analysis": "根据行情与持仓计算出的确定性分析指标。",
    "response": "投影到公开边界后的结构化响应。",
    "result": "内部步骤产生、等待后续投影的执行结果。",
    "parse_error": "显式指令无法解析时的原因；解析成功时为空。",
    "missing_fields": "工具入参仍缺少的必填字段名称。",
    "validation_errors": "工具入参模型返回的字段级校验错误。",
    "values": "目录初始化时接收的版本化配置或工具集合。",
    "entries": "按复合键索引的只读目录内容。",
    "records": "测试或内存实现持有的领域记录集合。",
    "enabled": "是否启用该可选能力。",
    "budget": "当前步骤可消耗的 token 或资源预算。",
    "PUBLIC": "无需访问控制即可公开的数据等级。",
    "INTERNAL": "仅限组织内部处理、不得公开披露的数据等级。",
    "CONFIDENTIAL": "需要严格访问控制的机密数据等级。",
    "RESTRICTED": "受最严格限制、通常不得发送给外部供应方的数据等级。",
    "ACTIVE": "记录当前有效，可继续读取或追加操作。",
    "ARCHIVED": "记录已归档，只保留历史查询用途。",
    "PENDING": "操作已创建但尚未开始处理。",
    "RUNNING": "操作正在执行且尚未产生终态结果。",
    "COMPLETED": "操作已成功完成并可读取最终结果。",
    "FAILED": "操作已失败，错误信息应记录在对应字段。",
    "INTERRUPTED": "运行停在可恢复检查点，等待外部决定。",
    "WAITING_CHILD": "父运行暂停推进，正在等待子委派完成。",
    "SUPERSEDED": "该版本已被新版本替代，不再作为当前有效记录。",
    "USER": "消息来自已认证用户。",
    "ASSISTANT": "消息由 Agent 或模型生成。",
    "READ": "工具只读取数据，不应改变外部状态。",
    "WRITE": "工具会创建或修改外部持久化状态。",
    "EXTERNAL_ACTION": "工具会触发现实世界或第三方系统动作。",
    "DELEGATION": "工具把任务移交给另一个 Agent 或工作流。",
    "NONE": "不启用该治理能力或没有对应副作用。",
    "IDEMPOTENT": "相同参数可安全重复执行并得到等价效果。",
    "KEY_REQUIRED": "只有携带稳定幂等键时才允许安全重试。",
    "LOW": "低风险，满足权限后通常可直接执行。",
    "MEDIUM": "中等风险，需要更严格审计或按策略审批。",
    "HIGH": "高风险，应在执行前取得明确人工审批。",
    "ALWAYS": "每次执行都必须经过人工审批。",
    "INTERNAL": "仅允许访问平台内部网络资源或处理内部级数据。",
    "EXTERNAL": "允许访问经出站策略批准的外部服务。",
    "DECISION": "只记录治理决策及其关键依据。",
    "EXECUTION": "同时记录治理决策与执行结果。",
    "FULL": "记录经脱敏的完整决策、输入与执行结果。",
    "TRANSIENT_READ": "仅对瞬时读取错误采用受限重试。",
    "ALLOW": "策略允许本次调用立即执行。",
    "DENY": "策略拒绝本次调用。",
    "REQUIRE_APPROVAL": "策略要求在执行前取得人工批准。",
    "APPROVE": "审批人同意按原参数继续执行。",
    "REJECT": "审批人拒绝执行并结束当前动作。",
    "EDIT": "审批人提供修改后的参数，再按新参数重新授权。",
    "TOOL": "显式调用目标是一个受治理工具。",
    "WORKFLOW": "显式调用或委派目标是确定性工作流。",
    "AGENT": "显式调用或委派目标是专业 Agent。",
    "DEVELOPMENT": "本地开发环境，允许使用便利性配置。",
    "TEST": "自动化测试环境，依赖应可替换且结果可复现。",
    "STAGING": "生产前验证环境，安全约束应接近生产。",
    "PRODUCTION": "生产环境，强制启用完整鉴权与网络安全校验。",
    "LOCAL": "制品内容写入受控的本地文件系统。",
    "S3": "制品内容写入 S3 兼容对象存储。",
    "type": "流事件类型，决定客户端如何解释 `data` 载荷。",
    "event": "SSE 事件名称，供客户端选择对应处理分支。",
    "data": "已经过 JSON 序列化的流事件载荷。",
    "finance_agent_stream_parts": "顶层 Agent 冒烟请求收到的流片段数量。",
    "direct_read_succeeded": "直接读取工具是否在无需审批的情况下成功完成。",
    "write_interrupted": "写工具首次调用是否按策略进入审批中断。",
    "edit_reinterrupted": "审批编辑参数后是否重新进入与新参数绑定的中断。",
    "write_approved": "写工具在批准后是否成功完成。",
    "input_hash": "原始输入的稳定哈希，用于证明数据来源未被替换。",
    "workflow_service": "负责启动、查询和恢复确定性工作流的应用服务。",
    "agent_profiles": "可按稳定标识和版本解析 Agent 配置的只读目录。",
    "delegation_service": "负责父运行与子目标之间状态协调的应用服务。",
    "summary_service": "负责构建和维护分层会话摘要的领域服务。",
    "approval_timeout": "人工审批允许等待的时长；超时后禁止恢复原检查点。",
    "should_read": "结构化探测输出中，模型是否判断当前请求需要读取行情。",
    "model_type": "实际创建的 LangChain 聊天模型实现类型。",
    "segment_messages": "生成一个最低层摘要所覆盖的消息数量。",
    "hierarchy_segments": "合并为一个高层摘要所需的相邻低层摘要数量。",
    "policy": "在副作用执行前作出确定性授权或记忆处理决定的策略。",
    "resolver": "把外部目标请求解析为固定版本运行参数的解析器。",
    "tool_catalog": "登记并解析所有可用受治理工具版本的目录。",
    "workflow_catalog": "登记并解析所有可用确定性工作流版本的目录。",
    "mode": "操作模式；决定撤销记录还是永久删除其内容。",
    "handle_tool_error": "是否由 LangChain 将工具异常转换为模型可见的错误消息。",
    "turns": "该会话通过 ORM 关系加载的轮次集合。",
    "conversation": "该记录所属的会话 ORM 对象。",
    "turn": "该消息所属的会话轮次 ORM 对象。",
    "access_policy": "读取制品所需满足的租户、主体或权限限制。",
    "item_type": "被上下文选择算法省略的条目类别。",
    "token_count": "该条目占用的估算 token 数。",
    "task": "交给子 Agent 或工作流处理的自然语言任务说明。",
    "context_refs": "父运行显式传给子目标的上下文引用。",
    "request_fingerprint": "委派或工作流请求的稳定指纹，用于幂等冲突检测。",
    "output_payload": "运行终态时保存的结构化输出快照。",
    "deployment_revision": "构建工作流图的部署修订号，用于定位实际运行代码。",
    "input_payload": "提交给工作流的规范化输入快照。",
    "artifact_refs": "本次运行、审计或事件关联的制品标识集合。",
    "request_payload": "审批点展示并绑定哈希的请求参数快照。",
    "required_scope": "作出该审批决定所需的权限域。",
    "model_factory": "依据模型配置创建主模型和回退模型的工厂。",
    "tool_policy": "在工具执行前作出允许、拒绝或需审批决定的策略。",
    "debug_full_io": "是否记录脱敏后的完整模型与工具输入输出；生产环境默认关闭。",
    "model_max_retries": "主模型及回退模型调用允许的最大重试次数。",
    "context_builder": "在 token 预算内构造可复现模型上下文的服务。",
    "conversation_repository": "维护会话 Journal、摘要和上下文清单的仓储。",
    "artifact_service": "决定结果内联或外置，并持久化制品元数据的服务。",
    "memory_service": "管理长期记忆生命周期与检索的领域服务。",
    "memory_recall_limit": "单次模型调用最多注入的长期记忆条数。",
    "settings": "应用启动时已校验的集中配置。",
    "model_profiles": "可按 ID 和版本解析模型配置的只读目录。",
    "agent_factory": "根据 Agent 配置组装完整运行时的工厂。",
    "database": "可选的数据库运行时；未启用持久化时为空。",
    "workflow_repository": "维护工作流运行与审批快照的仓储。",
    "delegation_repository": "维护父子运行委派状态的仓储。",
    "outbox_repository": "与业务事务协调写入和领取待发布事件的仓储。",
    "auto_commit_low_risk_preferences": "是否允许有直接用户证据的低风险偏好自动成为长期记忆。",
    "conversations": "用于验证记忆证据消息归属的会话仓储。",
    "model_input_limit": "模型上下文窗口允许的最大输入 token 数。",
    "system_policy_reserve": "为系统提示和不可裁剪策略预留的 token 数。",
    "tool_schema_reserve": "为模型可见工具 schema 预留的 token 数。",
    "safety_margin": "为分词误差和运行时附加内容保留的安全余量。",
    "input_schema": "工作流公开输入使用的 Pydantic 校验模型。",
    "output_schema": "工作流终态输出使用的 Pydantic 校验模型。",
    "timeout_policy": "工作流运行超时后采用的失败处理策略。",
    "producer": "产生该记忆内容的用户、Agent 或系统组件标识。",
    "record": "检索命中的完整长期记忆记录。",
    "metadata_json": "经 JSON 编码后持久化的附加审计元数据。",
    "aggregate_type": "产生 Outbox 事件的聚合根类别。",
    "locked_until": "事件领取租约的到期时间，防止多个发布者重复处理。",
    "environment": "当前部署环境，决定默认值和安全校验强度。",
    "provider_base_url": "模型供应商兼容 API 的基础 URL；为空时使用 SDK 默认地址。",
    "provider_api_key": "模型供应商 API 凭证，使用 SecretStr 避免意外日志泄露。",
    "offline_model": "是否使用确定性离线模型替代外部供应商。",
    "log_level": "应用根日志级别。",
    "read_max_attempts": "只读工具发生瞬时错误时允许的最大尝试次数。",
    "agent_server_url": "LangGraph Agent Server 的基础 URL。",
    "agent_server_service_token": "调用 Agent Server 时携带的可选服务凭证。",
    "oidc_issuer": "JWT 必须匹配的 OIDC 签发者。",
    "oidc_audience": "JWT 必须包含的目标受众。",
    "oidc_jwks_url": "获取 OIDC 公钥集合的 HTTPS 地址。",
    "oidc_algorithms": "验证 JWT 签名时允许的算法白名单。",
    "oidc_tenant_claim": "JWT 中承载租户标识的 claim 名称。",
    "oidc_subject_claim": "JWT 中承载主体标识的 claim 名称。",
    "oidc_scope_claim": "JWT 中承载权限域的 claim 名称。",
    "bff_auth_token": "非生产 BFF 模式使用的静态 Bearer 凭证。",
    "bff_scopes": "BFF 静态身份拥有的权限域。",
    "langsmith_project": "LangSmith 追踪写入的项目名称。",
    "langsmith_endpoint": "LangSmith API 端点。",
    "langsmith_trace_sample_rate": "LangSmith 链路追踪采样比例。",
    "langsmith_hide_inputs": "是否禁止向 LangSmith 发送原始输入。",
    "langsmith_hide_outputs": "是否禁止向 LangSmith 发送原始输出。",
    "otel_exporter_endpoint": "OpenTelemetry 追踪 OTLP HTTP 导出端点。",
    "otel_metrics_exporter_endpoint": "OpenTelemetry 指标 OTLP HTTP 导出端点。",
    "otel_trace_sample_rate": "OpenTelemetry 追踪采样比例。",
    "otel_service_name": "遥测数据中标识本服务的资源名称。",
    "database_url": "数据库连接 URL，使用 SecretStr 防止凭证进入日志。",
    "database_auto_create_schema": "启动时是否创建缺失表；只适合开发和测试。",
    "artifact_backend": "制品内容使用本地文件还是 S3 兼容存储。",
    "artifact_root": "本地制品存储的受控根目录。",
    "artifact_inline_bytes": "工具结果可直接内联返回的最大字节数。",
    "artifact_s3_bucket": "S3 制品存储桶；选择 S3 后端时必填。",
    "artifact_s3_prefix": "所有制品对象键使用的公共前缀。",
    "artifact_s3_endpoint_url": "自建 S3 兼容服务的可选端点。",
    "artifact_s3_region": "S3 客户端使用的区域。",
    "artifact_s3_sse_algorithm": "上传制品时请求的服务端加密算法。",
    "artifact_s3_max_pool_connections": "S3 HTTP 连接池允许的最大并发连接数。",
    "egress_allowed_hosts": "普通外部请求允许访问的主机白名单。",
    "internal_service_hosts": "平台内部服务主机白名单，可按策略允许私网地址。",
    "outbox_batch_size": "发布者单次领取的最大事件数量。",
    "outbox_max_attempts": "事件进入死信状态前允许的最大发布尝试次数。",
    "api_p95_target_ms": "HTTP 请求 P95 延迟目标，单位毫秒。",
    "context_input_limit": "模型上下文允许使用的最大输入 token 数。",
    "context_reserved_output": "为模型输出预留、不得被输入占用的 token 数。",
    "context_system_policy_reserve": "为系统策略内容预留的 token 数。",
    "context_tool_schema_reserve": "为工具 schema 预留的 token 数。",
    "context_safety_margin": "为分词估算误差预留的 token 安全余量。",
    "summary_segment_messages": "触发最低层摘要的消息分段大小。",
    "summary_hierarchy_segments": "合并为高层摘要的相邻摘要数量。",
    "memory_auto_commit_low_risk_preferences": "是否自动提交有证据支持的低风险偏好记忆。",
    "require_https": "是否拒绝所有非 HTTPS 出站 URL。",
    "allow_private_hosts": "是否允许目标解析为回环、链路本地或私网地址。",
    "trace_provider": "OpenTelemetry 追踪 Provider；未启用导出时为空。",
    "meter_provider": "OpenTelemetry 指标 Provider；未启用导出时为空。",
    "app": "被包装的下游 ASGI 应用。",
}

TOKEN_LABELS = {
    "Agent": "Agent",
    "Approval": "审批",
    "Artifact": "制品",
    "Audit": "审计",
    "Authenticated": "已认证",
    "Context": "上下文",
    "Contract": "契约",
    "Conversation": "会话",
    "Create": "创建",
    "Data": "数据",
    "Delegation": "委派",
    "Decision": "决策",
    "Direct": "直接",
    "Evaluation": "评测",
    "Event": "事件",
    "Execution": "执行",
    "Finance": "金融",
    "Frozen": "不可变",
    "Input": "输入",
    "Invocation": "调用",
    "Long": "长期",
    "Managed": "受治理",
    "Manifest": "清单",
    "Market": "行情",
    "Memory": "记忆",
    "Message": "消息",
    "Model": "模型",
    "Output": "输出",
    "Policy": "策略",
    "Portfolio": "投资组合",
    "Principal": "主体",
    "Profile": "配置",
    "Projection": "投影",
    "Proposal": "提议",
    "Reference": "引用",
    "Request": "请求",
    "Response": "响应",
    "Result": "结果",
    "Run": "运行",
    "Server": "服务端",
    "Slot": "参数槽位",
    "Source": "来源",
    "Status": "状态",
    "Summary": "摘要",
    "Tool": "工具",
    "Turn": "轮次",
    "Workflow": "工作流",
    "Watchlist": "观察列表",
}

METHOD_NAME_DOCS = {
    "trace_metadata": "生成不含原始租户和主体标识的追踪元数据；敏感标识仅输出截断哈希。",
    "digest": "计算不可逆的截断 SHA-256，避免在追踪标签中暴露原始标识。",
    "health": "调用轻量健康端点，返回依赖服务当前是否可用。",
    "key": "返回由稳定标识与版本组成的目录复合键。",
    "terminal": "判断当前状态是否已经进入不可继续推进的终态。",
    "requires_approval": "根据定义的审批点判断工作流是否可能产生人工中断。",
    "encryption_metadata": "返回可安全持久化的静态加密算法元数据。",
    "call_count": "返回测试行情工具累计被调用的次数。",
    "writes": "返回测试写工具已接收记录的不可变快照。",
    "text": "从消息对象提取可供预算计算和摘要使用的纯文本。",
    "message": "构造注明来源的系统消息，供模型上下文直接使用。",
    "truncate": "按 token 预算截断文本，并保留可识别的截断标记。",
    "content_hash": "对正文计算稳定 SHA-256，供完整性校验与去重使用。",
    "protect_production": "校验生产环境必须启用的鉴权、HTTPS 和安全配置。",
    "format": "把日志记录投影为单行 JSON，并递归脱敏异常和附加字段。",
    "publish": "调用事件发布函数；发布成功后由调用方负责更新持久化状态。",
    "run_once": "领取并尝试发布一批到期 Outbox 事件，返回成功发布数量。",
    "before_model": "把可信执行上下文的脱敏元数据写入当前追踪跨度。",
    "wrap_model_call": "在同步模型调用前后应用该中间件职责。",
    "awrap_model_call": "在异步模型调用前后应用该中间件职责。",
    "wrap_tool_call": "在同步工具调用前后应用该中间件职责。",
    "awrap_tool_call": "在异步工具调用前后应用该中间件职责。",
    "bind_tools": "记录绑定工具名称并返回支持后续调用的模型副本。",
    "_generate": "根据最新用户消息和已绑定工具确定性地产生聊天结果。",
    "_llm_type": "返回离线模型的稳定类型名，供 LangChain 序列化和诊断。",
    "project_sse": "把内部流事件编码为符合 SSE 语法的 `event` 与 `data` 数据帧。",
    "target_error": "将目标解析失败映射为 404 响应，并保留稳定错误码。",
    "run_error": "将运行不存在或不属于当前主体的情况映射为 404 响应。",
    "idempotency_error": "将幂等键与请求内容冲突映射为 409 响应。",
    "conversation_error": "将会话不存在或越权访问统一映射为 404 响应。",
    "conversation_conflict": "将会话状态或幂等冲突映射为 409 响应。",
    "approval_expired": "将已过审批期限的恢复请求映射为 409 响应。",
    "workflow_input": "将工作流输入校验失败映射为 422 响应。",
    "workflow_forbidden": "将工作流权限不足映射为 403 响应。",
    "workflow_conflict": "将工作流状态或审批冲突映射为 409 响应。",
    "workflow_approval_expired": "将工作流审批过期映射为 409 响应。",
    "delegation_input": "将委派目标或参数错误映射为 422 响应。",
    "delegation_forbidden": "将委派权限不足映射为 403 响应。",
    "delegation_conflict": "将委派状态机冲突映射为 409 响应。",
    "_require_internal_invocation": "要求身份含内部调用权限；缺少权限时立即拒绝管理型接口。",
    "lifespan": "在 FastAPI 启停边界执行启动准备与限时关闭钩子。",
    "ready": "并发运行全部就绪检查，仅在每项依赖均可用时返回成功。",
    "run_check": "为单个就绪检查施加超时，并把异常归一化为不可用。",
    "invoke_tool": "将直接工具 HTTP 请求转换为统一运行请求并交给运行服务。",
    "run_status": "根据运行所属服务查询状态，并在找不到时尝试其他运行类型。",
    "database_ready": "通过轻量查询检查数据库是否可接受请求。",
    "artifact_ready": "验证制品存储可用；当前实现确认服务已成功组装。",
    "default_agent_profile": "返回顶层金融 Agent 的固定配置，供图工厂和会话服务复用。",
    "seed_memory_dataset": "将长期记忆回归样例幂等发布到 LangSmith 数据集。",
    "seed_workflow_dataset": "将工作流回归样例幂等发布到 LangSmith 数据集。",
    "probe_agent_server": "顺序验证 Agent 流、直接读取、写入审批、编辑重审批和批准恢复。",
    "probe_provider": "验证模型工具调用、结构化输出、治理执行和追踪链路。",
    "probe_conversation": "创建真实会话并验证轮次启动、状态同步和消息 Journal。",
    "probe_memory": "通过真实会话验证记忆提议、确认、检索和跨轮注入。",
    "probe_workflow": "启动投资组合复核工作流，并验证中断审批与终态输出。",
    "_wait_for": "轮询运行状态直到命中预期状态或超过冒烟测试期限。",
    "_wait_for_interrupt": "轮询工作流，直到出现审批中断；超时或提前终结均视为失败。",
    "summarize_messages": "把连续原始消息压缩为结构化摘要，同时保留实体、决策与待办。",
    "summarize_summaries": "把相邻低层摘要合并为更高层摘要，并保留来源引用。",
    "rebuild": "清理旧摘要的当前态后，从消息 Journal 重新生成完整摘要层级。",
    "_message_summary": "把一段连续消息交给摘要器，并构造带序号范围和来源证据的摘要。",
    "_new_summary": "根据摘要内容、来源和版本生成确定性标识及完整领域记录。",
    "_bounded": "按字段上限截断并去重提取值，避免摘要元数据无限增长。",
    "_artifact_reference": "把制品服务元数据转换为工作流公开输出使用的引用字典。",
    "append_tool_audit": "为工作流内部工具授权或执行追加带运行关联的审计事件。",
    "route_freshness": "根据行情时效校验结果选择进入分析节点或失败终结节点。",
    "analyze": "使用 Decimal 计算组合市值、最大持仓权重和确定性风险分档。",
    "request_approval": "生成与输入哈希绑定的审批中断，等待用户确认发布报告。",
    "route_approval": "根据审批决定选择继续执行、拒绝终结或重新校验参数。",
    "finalize": "把图内部状态投影为满足终态 schema 的公开输出。",
    "portfolio_review_definition": "返回投资组合复核工作流的版本、权限、超时、schema 与图构建器。",
    "_approval_id": "由运行、目标和参数哈希生成稳定审批标识，确保恢复绑定原请求。",
    "append_audit": "为直接工具图的授权、审批和执行阶段追加不可变审计事件。",
    "authorize": "解析工具版本、校验参数并执行首次策略授权。",
    "route_authorization": "依据策略决策选择拒绝、请求审批或直接执行。",
    "approval": "创建与规范化参数哈希绑定的 LangGraph 人工审批中断。",
    "authorize_execution": "在审批恢复后对最终参数重新运行策略，防止编辑绕过治理。",
    "execute": "按重试策略调用工具，并把成功结果或最终错误写入图状态。",
    "project_response": "将直接工具图内部状态收敛为稳定响应，并按阈值外置大结果。",
    "make_finance_agent": "从运行配置组装并返回 LangGraph Server 使用的顶层金融 Agent。",
    "make_direct_tool_graph": "从运行配置组装并返回带治理和审批的直接工具图。",
    "begin_turn": "以客户端幂等键创建会话轮次与用户消息；安全重放时返回既有记录。",
    "bind_server_run": "将应用轮次或工作流运行与 Agent Server 运行标识原子绑定。",
    "append_assistant_message": "在轮次完成时幂等追加助手消息，并维护会话更新时间。",
    "append_branch_message": "在指定父消息后追加分支消息，同时分配新的全局序号。",
    "_conversation": "把会话 ORM 行转换为不可变领域记录。",
    "_turn": "把会话轮次 ORM 行转换为不可变领域记录。",
    "_message": "把消息 ORM 行转换为不可变领域记录。",
    "_summary": "把摘要 ORM 行及 JSON 字段转换为不可变领域记录。",
    "_manifest": "把上下文清单 ORM 行及引用字段转换为不可变领域记录。",
    "redact_sensitive": "递归遍历映射和序列，按键名遮盖令牌、密钥和授权信息。",
    "_jsonable": "把 Pydantic、映射和序列递归转换为 JSON 可序列化结构。",
    "_context": "从 LangChain 请求或工具运行时提取并校验可信执行上下文。",
    "trace_tool_authorization": "创建工具授权追踪节点，并返回策略决定供调用链记录。",
    "trace_context_prepare": "为模型上下文准备阶段创建可观测性追踪节点。",
    "_filter": "按当前身份与 Agent 白名单过滤模型可见工具，并记录过滤结果。",
    "_directive_denial": "构造显式调用被拒绝时的模型请求，使模型只能解释拒绝原因。",
    "_denied_message": "生成不泄露策略内部细节的稳定工具拒绝消息。",
    "_log": "在调试开关启用时记录方向和递归脱敏后的完整载荷。",
    "available_input_tokens": "扣除输出预留、系统策略、工具 schema 和安全余量后计算输入预算。",
    "_estimated_tokens": "使用缓存分词器估算文本 token；不可用时采用保守字符比例。",
    "_tiktoken_cache_available": "检查本地是否已有可用分词编码，避免为计数触发网络下载。",
    "_fit_runtime_suffix": "从最新消息向前选取完整运行片段，直到达到近期上下文预算。",
    "_current_runtime_suffix": "定位属于当前运行的消息后缀，避免把历史运行输出误作当前输入。",
    "_latest_user_text": "从模型请求或消息序列中提取最新一条用户文本。",
    "_matches_current": "判断消息是否属于当前运行、轮次或模型调用。",
    "_summary_message": "把领域摘要转换为带来源标识的系统消息。",
    "_terms": "规范化文本并提取用于轻量相关性计算的词项集合。",
    "_score": "计算查询词项与候选文本的重叠相关性得分。",
    "_rank_summaries": "按相关性、层级和时间范围稳定排序候选摘要。",
    "_rank_messages": "按相关性和消息序号稳定排序历史消息。",
    "__post_init__": "校验成对封装的工具标识与治理元数据完全一致。",
    "complete": "仅当参数已解析、没有缺失字段且没有校验错误时返回真。",
    "assess_tool_slots": "合并显式指令参数与工具 schema，返回缺失字段和校验错误。",
    "default_local_tools": "构造开发与测试使用的行情读取、观察列表写入和计算器工具集合。",
    "records": "返回仓储当前保存记录的不可变快照，主要用于测试与诊断。",
    "_record": "把 ORM 行或 Store 条目转换为不可变领域记录。",
    "begin_run": "以请求指纹幂等创建工作流运行；指纹冲突时拒绝复用幂等键。",
    "set_status": "以乐观锁推进运行或委派状态，并更新终态载荷与时间戳。",
    "ensure_approval": "幂等创建审批快照，并确保同一审批标识始终绑定相同请求哈希。",
    "decide_approval": "仅对待决审批写入决定，并通过请求哈希防止参数替换。",
    "_approval": "把审批 ORM 行转换为不可变工作流审批记录。",
    "_version_key": "把语义版本拆为整数元组，供目录选择最新版本。",
    "latest": "按工具标识分组，返回每个工具当前登记的最高语义版本。",
    "_metadata": "把制品 ORM 行转换为不可变元数据记录。",
    "ensure_requested": "以移交标识和请求指纹幂等创建委派，冲突时拒绝复用。",
    "prepare_agent_child": "为 Agent 委派生成并持久化稳定子线程标识。",
    "bind_child": "将委派记录与实际子运行原子绑定。",
    "latest_undelivered_for_parent": "查找父运行最近一个已完成但尚未交付结果的委派。",
    "_delegate": "连接 MCP 服务、加载指定工具并在超时限制内转发行情请求。",
    "managed_mcp_quote_tool": "构造带只读、外部出站和瞬时重试治理元数据的 MCP 行情工具。",
    "_draft": "把工具参数与可信运行上下文组合为长期记忆候选。",
    "_failure": "把已知记忆领域异常转换为模型可理解的稳定工具错误。",
    "default_memory_tools": "构造检索、提议、确认和遗忘四个受治理长期记忆工具。",
    "_apply": "根据当前模型请求准备附加上下文或工具限制，并返回更新后的请求。",
    "_override": "复制模型请求并仅替换指定字段，兼容不同 LangChain 请求实现。",
    "workflow_delegation_tool": "根据工作流定义生成可中断的委派工具及治理元数据。",
    "agent_delegation_tool": "根据专业 Agent 配置生成可中断的委派工具及治理元数据。",
    "_governance": "构造委派工具统一使用的权限、副作用、审批和审计策略。",
    "_handoff_id": "由父运行、工具调用和目标生成稳定移交标识，保证重放幂等。",
    "route_template": "把 ASGI 路由模板写入当前 span，避免按原始 URL 造成高基数。",
    "observed_send": "拦截响应开始与结束消息，记录状态码、首字节和总耗时。",
    "instrument_sqlalchemy_engine": "给数据库引擎注册查询耗时、计数与异常追踪监听器。",
    "before_cursor_execute": "在 SQL 执行前记录起始时间，并为当前 span 添加低基数操作名。",
    "after_cursor_execute": "在 SQL 成功后记录耗时指标。",
    "handle_error": "在 SQL 执行失败时记录错误计数和耗时。",
    "put": "写入制品二进制内容，并返回不含凭证的存储 URI。",
    "_artifact_id": "根据租户、主体和内容哈希生成确定性制品标识。",
    "_scope": "把租户与主体规范化为安全目录片段，防止路径穿越。",
    "_prepare": "构建模型消息、选择证据与清单，并用新上下文替换请求内容。",
    "trace_conversation_recall": "记录会话上下文候选检索的数量与选择结果。",
    "trace_manifest_persist": "记录上下文清单的持久化结果与关联标识。",
    "offload": "序列化工具结果；超过阈值时持久化并返回轻量制品引用。",
    "persist": "按内容哈希去重写入制品内容和元数据，返回稳定引用。",
    "read": "校验制品归属和访问策略后读取原始内容。",
    "_serialize": "将任意工具结果转换为稳定 JSON 字节或 UTF-8 文本。",
    "_provenance": "构造不含敏感输入的制品来源元数据。",
    "utcnow": "返回带 UTC 时区的当前时间，供 ORM 默认值统一使用。",
    "child_status": "根据委派目标类型查询子 Agent 或工作流的统一运行状态。",
    "latest_for_parent": "读取父运行最近一个尚未完成结果交付的委派。",
    "result": "返回已完成委派的规范化结果；未完成或失败时抛出状态冲突。",
    "_agent_status": "读取 Agent Server 子运行并映射为委派生命周期状态。",
    "_sync_child_status": "把统一子运行响应映射并写入委派状态机。",
    "_transition": "使用仓储乐观锁推进委派状态，并追加对应审计事件。",
    "_fail_start": "在子运行启动失败时将委派标记失败并记录原因。",
    "_resolve": "解析委派类型、固定目标版本、校验输入与所需权限。",
    "_verify_parent": "校验父运行、轮次、会话、租户和主体引用保持一致。",
    "_require_scopes": "比较所需与已有权限域，缺失任一权限时拒绝操作。",
    "extract_handoff_interrupt": "从 LangGraph 中断载荷中识别并校验委派请求。",
    "delegation_projection": "把委派记录投影为可放入运行响应的公开结构。",
    "_delegation_status": "把子运行状态字符串归一化为委派状态枚举。",
    "_final_assistant_content": "从服务端输出消息中提取最后一条助手文本。",
    "run_migrations_offline": "不连接数据库，根据 URL 和元数据生成迁移 SQL。",
    "run_migrations_online": "建立数据库连接，在事务中执行待应用的 Alembic 迁移。",
    "_advance_delegation": "推进活动子委派；完成后向父检查点交付结果并继续根运行。",
    "_conversation_response": "把内部会话记录转换为公开会话响应。",
    "_server_metadata": "组合执行上下文与阶段字段，生成不含敏感原值的服务端元数据。",
    "_jsonable_output": "把映射输出递归转换为可安全进入响应模型的 JSON 结构。",
    "claim_pending": "在事务中领取到期事件并设置短租约，避免并发发布者重复处理。",
    "_event": "把 Outbox ORM 行转换为不可变领域事件。",
    "_fingerprint": "对规范化运行请求计算稳定哈希，用于幂等冲突检测。",
    "_accepted": "把内部运行记录投影为已接受响应。",
    "_normalized_host": "规范化 URL 主机名，去除大小写和尾随点差异。",
    "assess": "根据记忆类型、敏感度和证据来源决定拒绝、确认或自动提交。",
    "_complete": "收敛工作流成功输出，持久化终态和制品引用并记录审计事件。",
    "_turn_id": "从工作流输入或运行标识派生稳定轮次关联标识。",
    "_response": "把工作流记录及可选审批投影为统一运行状态响应。",
    "_server_status": "把 Agent Server 状态字符串映射为工作流状态枚举。",
    "_aware": "为 SQLite 读出的无时区时间恢复 UTC 标记后再参与比较。",
    "_interrupt_payload": "从服务端任务中断结构提取第一个有效审批载荷。",
    "trace_memory_recall": "记录长期记忆检索数量与命中标识，不记录原始敏感内容。",
    "trace_memory_write": "记录长期记忆写入事件及稳定标识。",
    "namespace": "根据租户、主体和记忆类型构造隔离的 Store 命名空间。",
    "_require_store": "确认当前调用已配置长期记忆 Store，否则返回明确能力错误。",
    "_read": "从 Store 读取条目并转换为长期记忆记录。",
    "_project": "把长期记忆记录转换为不泄露内部 Store 结构的公开字典。",
    "_verify_scope": "校验记忆记录属于当前租户与主体。",
    "_put_operation": "构造可幂等重放的 Store 写操作描述。",
    "_same_facts": "比较候选与既有记录的事实字段，判断是否为安全重放。",
    "_proposal_id": "由作用域、事实和证据生成确定性记忆提议标识。",
    "_memory_id": "由作用域、事实与 schema 版本生成确定性长期记忆标识。",
    "_proposal_facts": "抽取决定提议身份的规范化事实字段。",
    "_conversation_id": "从证据消息解析并验证唯一会话归属。",
    "_now": "从可替换时间源取得带 UTC 时区的当前时间。",
    "_namespace_label": "将 Store 命名空间转换为可读标签，供审计和诊断使用。",
    "_relevance": "根据查询词项、记忆内容和状态计算轻量相关性得分。",
    "enable_sqlite_foreign_keys": "为每个新 SQLite 连接启用外键约束，保持测试与生产数据库语义一致。",
    "close": "释放数据库引擎及连接池持有的资源。",
    "ensure_database_parent": "为文件型 SQLite URL 创建缺失的父目录；其他数据库 URL 不做处理。",
    "authenticate": "验证 Bearer 凭证，并返回包含租户、主体和权限域的可信身份。",
    "_authenticate_sync": "同步验证 JWT 签名、签发者、受众与必要 claim，再构造可信身份。",
    "principal_dependency": "创建 FastAPI 身份依赖，从 Authorization 请求头提取并验证 Bearer 凭证。",
    "join_run": "等待指定服务端运行结束，并返回其最终结构化输出。",
    "append": "追加不可变审计事件；重复事件标识按仓储约定保持幂等。",
    "delegation_tool_name": "将委派类型与目标标识规范化为合法且稳定的工具名称。",
    "published": "返回所有已发布、可被新运行解析的工作流定义。",
}

METHOD_DOCS = {
    "ConversationService.create": "创建归属指定租户和主体的根会话，并固定当前根 Agent 配置版本。",
    "ConversationService.get": "读取调用主体拥有的会话，并转换为不泄露内部模型的响应。",
    "ConversationService.messages": "校验会话所有权后，按稳定序号返回该会话的可见消息。",
    "ConversationService.start_turn": "以幂等方式追加用户消息、创建会话轮次，并确保对应服务端运行已绑定。",
    "ConversationService.status": "汇总本地轮次、活动委派与服务端运行状态，并把可确认的终态写回 Journal。",
    "ConversationService.resume": "校验审批时效与参数绑定后，恢复当前委派或根 Agent 检查点。",
    "ConversationService.stream": "校验运行所有权后，将服务端线程事件转换为统一流事件。",
    "ConversationService.reconcile_incomplete": "扫描非终态轮次并逐一拉取远端状态，供启动恢复或定时修复使用。",
    "DelegationService.start": "解析并授权委派目标，幂等创建委派记录，再启动或复用对应子运行。",
    "DelegationService.status": "读取委派及其子运行状态，必要时推进状态机并持久化变化。",
    "DelegationService.resume": "把审批决定转交子工作流或 Agent，并同步委派状态。",
    "DelegationService.reconcile_incomplete": "扫描未完成委派并与子运行对账，返回本次成功推进的委派标识。",
    "WorkflowService.start": "校验权限与输入后幂等创建工作流运行，并确保服务端运行已提交和绑定。",
    "WorkflowService.status": "合并数据库记录与服务端状态，处理中断、超时和完成结果。",
    "WorkflowService.resume": "校验待决审批、过期时间与参数哈希后恢复工作流检查点。",
    "WorkflowService.reconcile_incomplete": "扫描未完成工作流并与 Agent Server 对账，修复可确定的状态。",
    "RunService.start": "解析调用目标，利用请求指纹实现进程内幂等，然后创建服务端运行。",
    "RunService.status": "读取调用主体拥有的运行，并返回服务端当前状态及可用输出。",
    "RunService.resume": "把人工审批决定转换为 LangGraph Command，恢复中断的服务端运行。",
    "RunService.stream": "校验运行所有权后转发服务端线程事件。",
    "TargetResolver.resolve": "校验目标类型和直接调用权限，解析固定版本并构造服务端输入。",
    "ApplicationDatabase.session": "打开事务会话；正常退出时提交，发生异常时回滚并始终关闭连接。",
    "ApplicationDatabase.initialize_schema": "在开发或测试启动路径中创建当前元数据尚不存在的表。",
    "ApplicationDatabase.dispose": "释放引擎持有的连接池资源。",
    "ApplicationDatabase.ping": "执行轻量查询验证数据库连接是否可用。",
    "ModelFactory.create": "解析模型配置，校验供应商凭证，并创建参数受限的聊天模型。",
    "ModelFactory.fallback_models": "按主配置声明的顺序创建回退模型链。",
    "EgressPolicy.validate": "规范化 URL，并依次校验 HTTPS、主机白名单和私网地址限制。",
    "RegressionGate.assert_passed": "校验结果覆盖所有用例、关键用例全部通过且总体得分达到门槛。",
    "TelemetryRuntime.shutdown": "按指标后追踪的顺序关闭 Provider，确保缓冲数据完成导出。",
    "OutboxPublisher.publish_batch": "领取一批到期事件，逐条发布，并分别记录成功或带退避的失败。",
    "ToolPolicy.evaluate": "依次检查租户、权限域和审批规则，返回可审计的确定性决策。",
    "ToolPolicy.visible": "判断工具是否应出现在当前调用主体可见的模型工具集合中。",
    "ToolPolicy.retryable": "仅允许声明为瞬时读取且满足幂等条件的失败重试。",
    "ToolGovernanceMiddleware._authorize": "提取执行上下文与规范化参数，解析工具并完成策略授权。",
    "ToolGovernanceMiddleware.wrap_tool_call": "在同步工具调用前授权并记录决定，调用后记录成功或失败。",
    "ToolGovernanceMiddleware.awrap_tool_call": "在异步工具调用前授权并记录决定，调用后记录成功或失败。",
    "ConversationContextBuilder.build": "按确定优先级填充输入预算，并返回消息列表、选择证据和清单。",
    "LongTermMemoryService.propose": "验证消息证据和记忆策略，创建待确认提议或直接提交低风险记忆。",
    "LongTermMemoryService.confirm": "校验提议归属及确认权限，写入新记忆并替代指定旧版本。",
    "LongTermMemoryService.recall": "按租户、主体、类型和查询检索有效记忆，并生成带理由的排序结果。",
    "LongTermMemoryService.forget": "校验归属后撤销或永久删除记忆，并留下对应审计事件。",
    "AgentFactory.build": "解析模型与工具白名单，按配置组装限额、治理、上下文和记忆中间件。",
    "MemoryRecallMiddleware._fit": "按相关性顺序选择记忆，直到达到条数或 token 预算上限。",
    "InvocationDirectiveMiddleware._apply": "解析最新用户指令；目标明确且参数完整时仅暴露对应工具。",
    "ToolResultArtifactMiddleware._project": "把工具响应交给制品服务，必要时替换为轻量引用。",
}

VERB_DOCS = {
    "create": "创建并返回新的{subject}。",
    "get": "按标识读取{subject}；不存在时由下层仓储抛出明确异常。",
    "list": "按稳定顺序列出满足条件的{subject}。",
    "save": "持久化{subject}并返回存储后的记录。",
    "add": "新增{subject}并维持关联约束。",
    "update": "更新{subject}，同时维护状态与时间戳约束。",
    "delete": "删除指定{subject}并返回删除结果。",
    "resolve": "解析并校验{subject}，返回固定版本的运行对象。",
    "validate": "校验{subject}的跨字段不变量并返回自身。",
    "build": "根据已注入依赖组装{subject}。",
    "start": "校验输入后启动{subject}，返回可供后续查询的记录。",
    "status": "读取并同步{subject}的当前状态。",
    "resume": "使用审批决定恢复中断的{subject}。",
    "stream": "校验访问权限后流式输出{subject}事件。",
    "search": "按查询条件检索{subject}并返回排序结果。",
    "find": "查找匹配的{subject}；没有匹配项时返回空值。",
    "publish": "发布{subject}并返回供应方结果。",
    "load": "从持久化表示加载并校验{subject}。",
    "parse": "解析外部表示并转换为{subject}。",
    "normalize": "将输入规范化为可比较、可持久化的{subject}。",
    "assert": "验证{subject}满足当前边界要求，否则抛出明确异常。",
    "mark": "以幂等方式标记{subject}的状态。",
    "record": "把已确认的{subject}事实持久化。",
    "configure": "配置{subject}，使后续运行统一采用该设置。",
    "install": "把{subject}安装到目标运行时。",
}


def module_doc(path: Path) -> str:
    key = path.as_posix()
    if key in FILE_DOCS:
        return FILE_DOCS[key]
    if "migrations/versions/" in key:
        return "定义该版本数据库结构变更及其可逆迁移步骤。"
    if "/migrations/env.py" in key:
        return "配置 Alembic 离线 SQL 生成与在线数据库迁移。"
    if "/operations/" in key:
        return f"提供 {path.stem.replace('_', ' ')} 运维命令的可调用入口。"
    if "/evaluation/" in key:
        return f"提供 {path.stem.replace('_', ' ')} 评测与发布能力。"
    for part, description in MODULE_DOCS.items():
        if part in path.parts:
            return description
    return "FinanceClaw 金融智能体平台包。"


def class_subject(name: str) -> str:
    explicit = {
        "Conversation": "一条根 Agent 会话",
        "ConversationTurn": "会话中的一次用户轮次及运行关联",
        "ConversationMessage": "会话 Journal 中的一条有序消息",
        "ConversationSummary": "一段连续消息或低层摘要的压缩表示",
        "ModelContextManifest": "一次模型调用实际可见内容的审计清单",
        "ContextSelection": "上下文选择算法输出的可序列化证据",
        "ExecutionContext": "跨 Agent、图与工具传递的可信调用上下文",
        "ArtifactReference": "外置制品的位置、大小和完整性元数据",
        "AuditEvent": "一次授权、执行或记忆变更的不可变审计事实",
        "DelegationRecord": "父运行向子目标移交任务的持久化状态",
        "LongTermMemory": "经证据和策略确认后可跨会话复用的长期事实",
        "MemoryProposal": "尚待策略或用户确认的长期记忆候选",
        "MemoryRecall": "带相关性与注入理由的长期记忆检索结果",
        "OutboxEvent": "与业务事务一起写入、等待可靠投递的领域事件",
        "WorkflowDefinition": "工作流构建器及其权限、超时和版本元数据",
        "WorkflowRun": "一次工作流执行的持久化状态与服务端关联",
        "WorkflowApproval": "工作流中断点对应的人工审批快照",
        "ManagedTool": "LangChain 工具及其不可分离的治理元数据",
        "ToolGovernance": "一个工具版本的静态安全与合规约束",
        "AgentProfile": "固定模型、工具和中间件行为的 Agent 版本配置",
        "ModelProfile": "固定供应商模型参数与数据边界的版本配置",
        "ResolvedTarget": "已固定版本并可提交执行的目标",
        "RunRecord": "进程内无会话运行的所有权和远端关联",
        "ServerRun": "Agent Server 创建运行后返回的最小引用",
    }
    if name in explicit:
        return explicit[name]
    label = translated_label(name)
    suffixes = {
        "Request": "接口请求",
        "Response": "边界响应",
        "Input": "校验输入",
        "Output": "稳定输出",
        "Projection": "公开投影",
        "Record": "持久化记录",
        "State": "图运行状态",
        "Table": "数据库映射",
        "Profile": "版本化配置",
        "Result": "执行结果",
        "Reference": "稳定引用",
    }
    for suffix, kind in suffixes.items():
        if name.endswith(suffix):
            stem = translated_label(name[: -len(suffix)])
            return f"{stem}的{kind}" if stem else kind
    return label


def translated_label(name: str) -> str:
    """Translate a CamelCase identifier into a compact domain label."""
    tokens = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", name.strip("_"))
    translated = [TOKEN_LABELS.get(token, token) for token in tokens]
    return "".join(translated) or name


def scenario_for(name: str, bases: list[str]) -> str:
    if name.endswith(("Error", "Expired", "Denied", "Conflict", "NotFound", "Unavailable")):
        return "用于把该失败条件跨层传递，并在接口边界转换为稳定错误。"
    if any(base.endswith(("StrEnum", "Enum")) for base in bases):
        return "用于限制持久化值和边界输入，避免以自由字符串表达状态。"
    if any("Protocol" in base for base in bases):
        return "用于依赖倒置和测试替身，使应用逻辑不依赖具体客户端实现。"
    if any("TypedDict" in base for base in bases):
        return "用于图节点之间共享结构化字典，同时保留静态类型提示。"
    for suffix, scenario in CLASS_SCENARIOS.items():
        if name.endswith(suffix):
            return scenario
    if "BaseModel" in " ".join(bases) or "Frozen" in " ".join(bases):
        return "用于在接口、领域与持久化边界之间传递经过校验的结构化数据。"
    return "用于集中表达该职责，避免调用方直接依赖底层实现细节。"


def collect_attrs(node: ast.ClassDef) -> list[str]:
    attrs: list[str] = []
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            attrs.append(item.target.id)
        elif isinstance(item, ast.Assign):
            attrs.extend(
                target.id
                for target in item.targets
                if isinstance(target, ast.Name) and target.id not in {"__slots__"}
            )
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
            for child in ast.walk(item):
                if (
                    isinstance(child, (ast.Assign, ast.AnnAssign))
                    and isinstance(child.target if isinstance(child, ast.AnnAssign) else child.targets[0], ast.Attribute)
                ):
                    target = child.target if isinstance(child, ast.AnnAssign) else child.targets[0]
                    if isinstance(target.value, ast.Name) and target.value.id == "self":
                        attrs.append(target.attr)
    return list(dict.fromkeys(attrs))


def field_doc(name: str) -> str:
    plain = name.lstrip("_")
    if plain in FIELD_DOCS:
        return FIELD_DOCS[plain]
    if plain.endswith("_id"):
        return "关联对象的稳定标识，用于查询、关联和审计追踪。"
    if plain.endswith("_ids"):
        return "关联对象标识的有序集合。"
    if plain.endswith("_version"):
        return "运行固定使用的版本，用于审计复现。"
    if plain.endswith("_at"):
        return "该生命周期事件发生的 UTC 时间。"
    if plain.endswith("_seconds"):
        return "该操作允许的最长时间（秒）。"
    if plain.endswith("_tokens") or plain.endswith("_token_count"):
        return "该步骤可用或实际使用的 token 数量。"
    if plain.startswith("max_"):
        return "限制该资源或操作的最大允许值。"
    if plain.startswith("allowed_"):
        return "当前配置明确允许的值集合。"
    if plain.startswith("supports_"):
        return "供应方或运行目标是否支持该能力。"
    if plain.startswith("is_") or plain.startswith("has_"):
        return "标记当前对象是否满足对应条件。"
    if plain.startswith("_remaining_"):
        return "测试替身仍需模拟失败的次数。"
    if plain.startswith("_call_count"):
        return "该测试工具实例累计执行次数。"
    if plain.startswith("_writes"):
        return "测试工具已接收的写入记录，用于断言副作用。"
    if plain.isupper():
        return f"表示 `{plain.lower()}` 这一受限枚举值。"
    readable = plain.replace("_", " ")
    if name.startswith("_"):
        return f"内部 `{readable}` 状态或依赖，不属于公开接口。"
    return f"业务字段 `{readable}`；类型声明限定其结构，模型校验补充跨字段约束。"


def class_doc(node: ast.ClassDef) -> str:
    bases = [ast.unparse(base) for base in node.bases]
    if node.name in ENUM_ROLES:
        role = ENUM_ROLES[node.name]
    else:
        role = CLASS_ROLES.get(node.name, f"定义{class_subject(node.name)}。")
    attrs = collect_attrs(node)
    pieces = [role, "", "适用场景：", f"    {scenario_for(node.name, bases)}"]
    if attrs:
        pieces.extend(["", "属性："])
        for attr in attrs:
            pieces.append(f"    {attr}: {field_doc(attr)}")
    return "\n".join(pieces)


def function_subject(path: Path, parent: ast.ClassDef | None) -> str:
    if parent is not None:
        if parent.name.endswith("Repository"):
            domain = path.parent.name
            labels = {
                "artifacts": "制品记录",
                "audit": "审计事件",
                "conversation": "会话 Journal 记录",
                "delegation": "委派记录",
                "outbox": "Outbox 事件",
                "workflows": "工作流运行",
            }
            return labels.get(domain, "领域记录")
        if parent.name.endswith("Catalog"):
            return class_subject(parent.name).replace("目录", "目录项")
        return class_subject(parent.name)
    stem = path.stem.replace("_", " ")
    return f"{stem} 模块的数据"


def function_doc(path: Path, node: ast.FunctionDef | ast.AsyncFunctionDef, parent: ast.ClassDef | None) -> str:
    qualified = f"{parent.name}.{node.name}" if parent else node.name
    if qualified in METHOD_DOCS:
        return METHOD_DOCS[qualified]
    name = node.name
    subject = function_subject(path, parent)
    if name in METHOD_NAME_DOCS:
        return METHOD_NAME_DOCS[name]
    if name == "__init__":
        return f"注入并保存{subject}所需的协作对象，同时校验构造期不变量。"
    if name in {"__getitem__", "__iter__", "__len__", "__getattr__"}:
        special = {
            "__getitem__": "按键读取目录项，保持 Mapping 接口语义。",
            "__iter__": "按目录内部的稳定顺序迭代所有键。",
            "__len__": "返回目录当前登记的条目数量。",
            "__getattr__": "按需导入并返回公开符号，避免包初始化阶段形成循环依赖。",
        }
        return special[name]
    if name == "__call__":
        return f"以可调用对象形式处理{subject}，并把请求交给下一处理阶段。"
    if name in {"upgrade", "downgrade"}:
        action = "创建本版本新增的表、索引和约束" if name == "upgrade" else "按依赖逆序移除本版本引入的数据库对象"
        return f"{action}。"
    if name == "main":
        return f"解析命令行参数，执行 {path.stem.replace('_', ' ')} 操作并输出结果。"
    if name.startswith("_run"):
        return "执行工具的同步实现，并返回可序列化结果。"
    if name.startswith("_arun"):
        return "执行工具的异步实现，保持与同步入口相同的业务语义。"
    if "validator" in " ".join(ast.unparse(d) for d in node.decorator_list) or "must_" in name:
        return f"校验{subject}的跨字段一致性；不满足不变量时拒绝构造。"
    for prefix, template in VERB_DOCS.items():
        if name == prefix or name.startswith(prefix + "_") or name.startswith("_" + prefix + "_"):
            return template.format(subject=subject)
    if name.startswith("_to_") or name.startswith("to_"):
        return f"把内部值转换为{subject}所需的边界表示。"
    if name.startswith("_from_") or name.startswith("from_"):
        return f"从持久化或传输表示重建{subject}。"
    if name.startswith("_hash") or name.endswith("_hash"):
        return "对规范化内容计算稳定 SHA-256，供幂等、审批绑定或审计使用。"
    if name.startswith("_audit"):
        return "构造不可变审计事件并写入审计仓储。"
    if name.startswith("_owned") or "owned" in name:
        return "读取记录并同时校验租户与主体所有权，避免越权访问。"
    if name.startswith("_is_") or name.startswith("is_"):
        return f"判断{subject}是否满足对应条件。"
    if name.startswith("_"):
        readable = name.strip("_").replace("_", " ")
        return f"完成内部 `{readable}` 转换或校验，结果仅供相邻编排步骤使用。"
    return f"执行 `{name.replace('_', ' ')}` 操作，返回经过类型约束的{subject}结果。"


def docstring_lines(doc: str, indent: str) -> list[str]:
    width = max(48, 96 - len(indent))
    paragraphs = doc.split("\n")
    rendered: list[str] = []
    in_attrs = False
    for paragraph in paragraphs:
        if not paragraph:
            rendered.append("")
            continue
        if paragraph in {"适用场景：", "属性："}:
            rendered.append(paragraph)
            in_attrs = paragraph == "属性："
            continue
        if paragraph.startswith("    "):
            initial = "    "
            subsequent = "        " if in_attrs and ": " in paragraph else "    "
            rendered.extend(
                textwrap.wrap(
                    paragraph.strip(),
                    width=width,
                    initial_indent=initial,
                    subsequent_indent=subsequent,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
        else:
            rendered.extend(
                textwrap.wrap(
                    paragraph,
                    width=width,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
    if len(rendered) == 1:
        return [f'{indent}"""{rendered[0]}"""\n']
    return [f'{indent}"""{rendered[0]}\n'] + [
        f"{indent}{line}\n" if line else "\n" for line in rendered[1:]
    ] + [f'{indent}"""\n']


def nodes_with_parents(tree: ast.AST):
    """Yield documented nodes while retaining the nearest containing class."""

    yield tree, None

    def walk(node: ast.AST, parent: ast.ClassDef | None):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield child, None
                yield from walk(child, child)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child, parent
                yield from walk(child, parent)
            else:
                yield from walk(child, parent)

    yield from walk(tree, None)


def rewrite(path: Path, *, strip_comments: bool = False) -> str:
    original = path.read_text()
    lines = original.splitlines(keepends=True)
    if strip_comments:
        lines = [line for line in lines if not line.lstrip().startswith("#")]
        original = "".join(lines)
    tree = ast.parse(original)
    replacements: list[tuple[int, int, list[str]]] = []
    for node, parent in nodes_with_parents(tree):
        if not getattr(node, "body", None):
            continue
        first = node.body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        indent = "" if isinstance(node, ast.Module) else " " * (node.col_offset + 4)
        if isinstance(node, ast.Module):
            doc = module_doc(path)
        elif isinstance(node, ast.ClassDef):
            doc = class_doc(node)
        else:
            doc = function_doc(path, node, parent)
        replacements.append((first.lineno - 1, first.end_lineno, docstring_lines(doc, indent)))
    for start, end, new_lines in sorted(replacements, reverse=True):
        lines[start:end] = new_lines
    return "".join(lines)


def emit_patch(paths: list[Path], *, strip_comments: bool = False) -> None:
    print("*** Begin Patch")
    for path in paths:
        old = path.read_text()
        new = rewrite(path, strip_comments=strip_comments)
        if old == new:
            continue
        diff = list(
            difflib.unified_diff(
                old.splitlines(),
                new.splitlines(),
                fromfile=str(path),
                tofile=str(path),
                lineterm="",
            )
        )
        print(f"*** Update File: {path}")
        for line in diff[2:]:
            # The workspace apply_patch dialect uses a bare section marker and
            # locates hunks from their context rather than GNU line ranges.
            print("@@" if line.startswith("@@") else line)
    print("*** End Patch")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strip-comments", action="store_true")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    paths: list[Path] = []
    for value in args.paths:
        path = Path(value)
        paths.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    emit_patch(list(dict.fromkeys(paths)), strip_comments=args.strip_comments)


if __name__ == "__main__":
    main()
