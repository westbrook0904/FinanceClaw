"""Agent 工厂：依据 AgentProfile 在启动期装配受治理的 ReAct Agent。

属于 orchestration.agents 的装配入口：按档案解析允许工具与模型档案，串联工具
治理、调用偏好指令、工件 offload、记忆召回、上下文组装、兜底重试与调用限额
等中间件，最终经 LangChain create_agent 构建可运行的顶层 Agent。

"""

from collections.abc import Mapping
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    ModelFallbackMiddleware,
    ModelRetryMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.base import BaseStore

from financeclaw.infrastructure.llm import ModelFactory
from financeclaw.kernel import ExecutionContext
from financeclaw.modules.artifacts import ArtifactService
from financeclaw.modules.audit import AuditRepository
from financeclaw.modules.conversation import ConversationContextBuilder, ConversationRepository
from financeclaw.modules.memory import LongTermMemoryService
from financeclaw.orchestration.tools import (
    ApprovalMode,
    RetryProfile,
    ToolCatalog,
    ToolDecisionType,
    ToolPolicy,
    TransientToolError,
)

from .artifact_middleware import ToolResultArtifactMiddleware
from .context_middleware import ConversationContextMiddleware
from .directive_middleware import InvocationDirectiveMiddleware
from .memory_middleware import MemoryRecallMiddleware
from .middleware import ContextTraceMiddleware, FullIODebugMiddleware, ToolGovernanceMiddleware
from .profiles import AgentProfile

# build 的 checkpointer 参数哨兵值：调用方未显式传入时使用内存 Checkpointer。
_DEFAULT_CHECKPOINTER = object()


class AgentFactory:
    """依据 AgentProfile 装配完整中间件栈的 ReAct Agent 工厂。

    使用场景：应用启动期构造一次并持有；每次需要 Agent 时调用 build 传入档案
    （如 finance_agent），得到挂满治理中间件的顶层 ReAct Agent，负责判断直接
    回答、Tool Calling、Workflow handoff 或领域 Agent delegation。

    Attributes:
        model_factory: 模型工厂，用于创建主模型、解析模型档案与兜底模型。
        tool_catalog: 工具目录，按 id 与版本解析受治理的受管工具。
        tool_policy: 工具策略，评估可见性、放行、拒绝与审批决策。
        audit: 审计仓储，记录工具授权与执行事件。
        debug_full_io: 是否开启完整输入输出调试日志与完整 Prompt 明文。
        model_max_retries: 模型调用瞬时失败的最大重试次数，默认 2。
        context_builder: 会话上下文构建器；为 None 时不挂载上下文中间件。
        conversation_repository: 会话仓储；为 None 时不挂载上下文中间件。
        artifact_service: Artifact 服务；为 None 时不挂载工件 offload 中间件。
        memory_service: 长期记忆服务；为 None 时不挂载记忆召回中间件。
        memory_recall_tokens: 记忆召回区域的独立 token 预算，默认 768。
        memory_recall_limit: 单次召回注入模型上下文的记忆条数上限，默认 5。

    """

    def __init__(
        self,
        *,
        model_factory: ModelFactory,
        tool_catalog: ToolCatalog,
        tool_policy: ToolPolicy,
        audit: AuditRepository,
        debug_full_io: bool,
        model_max_retries: int = 2,
        context_builder: ConversationContextBuilder | None = None,
        conversation_repository: ConversationRepository | None = None,
        artifact_service: ArtifactService | None = None,
        memory_service: LongTermMemoryService | None = None,
        memory_recall_tokens: int = 768,
        memory_recall_limit: int = 5,
    ) -> None:
        """保存全部装配依赖，供后续按档案构建 Agent 时使用。

        Args:
            model_factory: 模型工厂。
            tool_catalog: 工具目录。
            tool_policy: 工具策略。
            audit: 审计仓储。
            debug_full_io: 是否开启完整输入输出调试日志。
            model_max_retries: 模型调用最大重试次数。
            context_builder: 会话上下文构建器，可选。
            conversation_repository: 会话仓储，可选。
            artifact_service: Artifact 服务，可选。
            memory_service: 长期记忆服务，可选。
            memory_recall_tokens: 记忆召回 token 预算。
            memory_recall_limit: 记忆召回条数上限。

        """
        self.model_factory = model_factory
        self.tool_catalog = tool_catalog
        self.tool_policy = tool_policy
        self.audit = audit
        self.debug_full_io = debug_full_io
        self.model_max_retries = model_max_retries
        self.context_builder = context_builder
        self.conversation_repository = conversation_repository
        self.artifact_service = artifact_service
        self.memory_service = memory_service
        self.memory_recall_tokens = memory_recall_tokens
        self.memory_recall_limit = memory_recall_limit

    def build(
        self,
        profile: AgentProfile,
        *,
        model: BaseChatModel | None = None,
        fallback_models: tuple[BaseChatModel, ...] | None = None,
        checkpointer: Any = _DEFAULT_CHECKPOINTER,
        store: BaseStore | None = None,
    ) -> Any:
        """按档案装配带完整中间件栈的 ReAct Agent。

        Args:
            profile: Agent 档案，声明允许工具、模型档案、系统提示与限额。
            model: 显式指定的主模型；缺省时按档案的 model_profile 创建。
            fallback_models: 显式指定的兜底模型；缺省时按模型档案解析。
            checkpointer: LangGraph Checkpointer；未显式传入时使用内存实现。
            store: LangGraph BaseStore，供记忆等设施使用；缺省为 None。

        Returns:
            Any: 装配完成的 ReAct Agent（LangGraph 编译结果）。

        """
        # 1. 依据档案解析受治理工具，并得到允许键集合（tool_id@version）。
        resolved_tools = tuple(
            self.tool_catalog.resolve(ref.tool_id, ref.version) for ref in profile.allowed_tools
        )
        allowed_keys = frozenset(managed.key for managed in resolved_tools)
        # 2. 解析主模型、模型档案与兜底模型列表。
        primary = model or self.model_factory.create(profile.model_profile)
        model_profile = self.model_factory.catalog.resolve(profile.model_profile)
        fallbacks = (
            fallback_models
            if fallback_models is not None
            else self.model_factory.fallback_models(model_profile)
        )
        # 3. 收集按瞬时失败重试策略配置的只读工具名，供后续挂载重试中间件。
        read_retry_tools = [
            managed.tool.name
            for managed in resolved_tools
            if managed.governance.retry_profile is RetryProfile.TRANSIENT_READ
        ]

        def requires_approval(request: Any) -> bool:
            """人工审批判定：依据工具策略评估该调用是否需要审批。"""
            raw_context = request.runtime.context
            context = (
                raw_context
                if isinstance(raw_context, ExecutionContext)
                else ExecutionContext.model_validate(raw_context)
            )
            tool_call = request.tool_call
            try:
                managed = self.tool_catalog.resolve(str(tool_call["name"]))
            except LookupError:
                return False
            # 不在档案允许集合内的工具不触发审批，由治理中间件拒绝。
            if managed.key not in allowed_keys:
                return False
            arguments = tool_call.get("args", {})
            if not isinstance(arguments, Mapping):
                return False
            decision = self.tool_policy.evaluate(context, managed.governance, dict(arguments))
            return decision.effect is ToolDecisionType.REQUIRE_APPROVAL

        # 4. 为 ALWAYS 审批模式的工具构造人工审批中断配置。
        interrupt_on = {
            managed.tool.name: {
                "allowed_decisions": ["approve", "reject"],
                "description": "FinanceClaw WRITE approval required",
                "when": requires_approval,
            }
            for managed in resolved_tools
            if managed.governance.approval is ApprovalMode.ALWAYS
        }
        # 5. 按顺序装配治理类中间件：人工审批、工具治理与调用偏好指令。
        middleware: list[Any] = []
        if interrupt_on:
            middleware.append(
                HumanInTheLoopMiddleware(
                    interrupt_on=interrupt_on,
                    description_prefix="FinanceClaw governed action",
                )
            )
        middleware.extend(
            [
                ToolGovernanceMiddleware(
                    self.tool_catalog,
                    self.tool_policy,
                    self.audit,
                    allowed_keys=allowed_keys,
                ),
                InvocationDirectiveMiddleware(),
            ]
        )
        # 6. 可选挂载工件 offload、记忆召回与会话上下文中间件。
        if self.artifact_service is not None:
            middleware.append(ToolResultArtifactMiddleware(self.artifact_service))
        if self.memory_service is not None and profile.memory_policy != "none":
            middleware.append(
                MemoryRecallMiddleware(
                    self.memory_service,
                    max_tokens=self.memory_recall_tokens,
                    max_memories=self.memory_recall_limit,
                )
            )
        if self.context_builder is not None and self.conversation_repository is not None:
            middleware.append(
                ConversationContextMiddleware(
                    builder=self.context_builder,
                    repository=self.conversation_repository,
                    tool_catalog=self.tool_catalog,
                    agent_profile_version=profile.version,
                    model_profile_version=model_profile.version,
                    prompt_template_version=f"{profile.agent_id}-system/{profile.version}",
                    debug_full_io=self.debug_full_io,
                )
            )
        # 7. 挂载追踪与输入输出调试中间件。
        middleware.extend(
            [
                ContextTraceMiddleware(),
                FullIODebugMiddleware(enabled=self.debug_full_io),
            ]
        )
        # 8. 挂载模型兜底与模型瞬时失败重试中间件。
        if fallbacks:
            middleware.append(ModelFallbackMiddleware(*fallbacks))
        middleware.append(
            ModelRetryMiddleware(
                max_retries=self.model_max_retries,
                initial_delay=0.1,
                jitter=False,
                on_failure="error",
            )
        )
        # 9. 只读工具存在时挂载瞬时错误重试中间件。
        if read_retry_tools:
            middleware.append(
                ToolRetryMiddleware(
                    max_retries=2,
                    tools=read_retry_tools,
                    retry_on=TransientToolError,
                    initial_delay=0,
                    jitter=False,
                    on_failure="error",
                )
            )
        # 10. 挂载模型与工具的运行内调用限额中间件。
        middleware.extend(
            [
                ModelCallLimitMiddleware(run_limit=profile.max_model_calls, exit_behavior="error"),
                ToolCallLimitMiddleware(run_limit=profile.max_tool_calls, exit_behavior="error"),
            ]
        )
        # 11. 解析 Checkpointer（缺省用内存实现），交给 create_agent 装配。
        resolved_checkpointer = (
            InMemorySaver() if checkpointer is _DEFAULT_CHECKPOINTER else checkpointer
        )
        return create_agent(
            model=primary,
            tools=[managed.tool for managed in resolved_tools],
            system_prompt=profile.system_prompt_template,
            middleware=middleware,
            context_schema=ExecutionContext,
            checkpointer=resolved_checkpointer,
            store=store,
            name=profile.agent_id,
        )
