"""Build the single governed finance_agent on LangChain/LangGraph."""

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

from financeclaw.audit import AuditRepository
from financeclaw.contracts import ExecutionContext
from financeclaw.models import ModelFactory
from financeclaw.tools import (
    ApprovalMode,
    RetryProfile,
    ToolCatalog,
    ToolDecisionType,
    ToolPolicy,
    TransientToolError,
)

from .middleware import ContextTraceMiddleware, FullIODebugMiddleware, ToolGovernanceMiddleware
from .profiles import AgentProfile

_DEFAULT_CHECKPOINTER = object()


class AgentFactory:
    def __init__(
        self,
        *,
        model_factory: ModelFactory,
        tool_catalog: ToolCatalog,
        tool_policy: ToolPolicy,
        audit: AuditRepository,
        debug_full_io: bool,
        model_max_retries: int = 2,
    ) -> None:
        self.model_factory = model_factory
        self.tool_catalog = tool_catalog
        self.tool_policy = tool_policy
        self.audit = audit
        self.debug_full_io = debug_full_io
        self.model_max_retries = model_max_retries

    def build(
        self,
        profile: AgentProfile,
        *,
        model: BaseChatModel | None = None,
        fallback_models: tuple[BaseChatModel, ...] | None = None,
        checkpointer: Any = _DEFAULT_CHECKPOINTER,
    ) -> Any:
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
            name=profile.agent_id,
        )
