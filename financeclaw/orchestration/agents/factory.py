"""依据 Agent 配置组装模型、工具与治理中间件。"""

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

_DEFAULT_CHECKPOINTER = object()


class AgentFactory:
    """将 Agent 配置解析为模型、工具、限额及中间件组成的运行实例。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        model_factory: 依据模型配置创建主模型和回退模型的工厂。
        tool_catalog: 登记并解析所有可用受治理工具版本的目录。
        tool_policy: 在工具执行前作出允许、拒绝或需审批决定的策略。
        audit: 记录授权、执行和状态变化的审计仓储。
        debug_full_io: 是否记录脱敏后的完整模型与工具输入输出；生产环境默认关闭。
        model_max_retries: 主模型及回退模型调用允许的最大重试次数。
        context_builder: 在 token 预算内构造可复现模型上下文的服务。
        conversation_repository: 维护会话 Journal、摘要和上下文清单的仓储。
        artifact_service: 决定结果内联或外置，并持久化制品元数据的服务。
        memory_service: 管理长期记忆生命周期与检索的领域服务。
        memory_recall_tokens: 该步骤可用或实际使用的 token 数量。
        memory_recall_limit: 单次模型调用最多注入的长期记忆条数。
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
        """注入并保存AgentFactory所需的协作对象，同时校验构造期不变量。"""
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
        """解析模型与工具白名单，按配置组装限额、治理、上下文和记忆中间件。"""
        resolved_tools = tuple(
            self.tool_catalog.resolve(ref.tool_id, ref.version) for ref in profile.allowed_tools
        )
        allowed_keys = frozenset(managed.key for managed in resolved_tools)
        primary = model or self.model_factory.create(profile.model_profile)
        model_profile = self.model_factory.catalog.resolve(profile.model_profile)
        fallbacks = (
            fallback_models
            if fallback_models is not None
            else self.model_factory.fallback_models(model_profile)
        )
        read_retry_tools = [
            managed.tool.name
            for managed in resolved_tools
            if managed.governance.retry_profile is RetryProfile.TRANSIENT_READ
        ]

        def requires_approval(request: Any) -> bool:
            """根据定义的审批点判断工作流是否可能产生人工中断。"""
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
            if managed.key not in allowed_keys:
                return False
            arguments = tool_call.get("args", {})
            if not isinstance(arguments, Mapping):
                return False
            decision = self.tool_policy.evaluate(context, managed.governance, dict(arguments))
            return decision.effect is ToolDecisionType.REQUIRE_APPROVAL

        interrupt_on = {
            managed.tool.name: {
                "allowed_decisions": ["approve", "reject"],
                "description": "FinanceClaw WRITE approval required",
                "when": requires_approval,
            }
            for managed in resolved_tools
            if managed.governance.approval is ApprovalMode.ALWAYS
        }
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
        middleware.extend(
            [
                ContextTraceMiddleware(),
                FullIODebugMiddleware(enabled=self.debug_full_io),
            ]
        )
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
        middleware.extend(
            [
                ModelCallLimitMiddleware(run_limit=profile.max_model_calls, exit_behavior="error"),
                ToolCallLimitMiddleware(run_limit=profile.max_tool_calls, exit_behavior="error"),
            ]
        )
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
