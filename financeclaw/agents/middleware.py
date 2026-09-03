"""Tool governance, bounded debug I/O and LangSmith context middleware."""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from hashlib import sha256
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, ToolMessage
from langsmith import traceable

from financeclaw.audit import AuditEventType, AuditRecord, AuditRepository
from financeclaw.contracts import ExecutionContext
from financeclaw.tools import ToolCatalog, ToolDecision, ToolDecisionType, ToolPolicy

from .directives import (
    InvocationDirective,
    InvocationKind,
    assess_tool_slots,
    parse_invocation_directive,
)

LOGGER = logging.getLogger("financeclaw.model_io")

_SENSITIVE_KEYS = re.compile(
    r"(?:authorization|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|password|dsn|credential|reasoning|thinking|hidden)",
    re.IGNORECASE,
)
_SECRET_VALUES = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk|pk|lsv2_pt)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?:postgres(?:ql)?|redis)://[^\s]+", re.IGNORECASE),
)


def canonical_arguments_hash(arguments: Mapping[str, Any]) -> str:
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode()).hexdigest()


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if _SENSITIVE_KEYS.search(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        for pattern in _SECRET_VALUES:
            value = pattern.sub("<redacted>", value)
    return value


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [_jsonable(item) for item in value]
    return repr(value)


def _context(value: Any) -> ExecutionContext:
    if isinstance(value, ExecutionContext):
        return value
    if isinstance(value, Mapping):
        return ExecutionContext.model_validate(value)
    raise TypeError("trusted ExecutionContext is required")


@traceable(name="tool.authorization", run_type="chain", tags=["stage:1"])
def trace_tool_authorization(
    *, tool_id: str, effect: str, arguments_hash: str, context_metadata: dict[str, str]
) -> None:
    del tool_id, effect, arguments_hash, context_metadata


@traceable(name="context.prepare", run_type="chain", tags=["stage:1"])
def trace_context_prepare(*, message_count: int, context_metadata: dict[str, str]) -> None:
    del message_count, context_metadata


class ContextTraceMiddleware(AgentMiddleware):
    def before_model(self, state: Any, runtime: Any) -> None:
        context = _context(runtime.context)
        trace_context_prepare(
            message_count=len(state.get("messages", ())),
            context_metadata=context.trace_metadata(),
        )


class ToolGovernanceMiddleware(AgentMiddleware):
    """Filter model-visible tools and re-authorize every actual call."""

    def __init__(
        self,
        catalog: ToolCatalog,
        policy: ToolPolicy,
        audit: AuditRepository,
        *,
        allowed_keys: frozenset[tuple[str, str]],
    ) -> None:
        super().__init__()
        self.catalog = catalog
        self.policy = policy
        self.audit = audit
        self.allowed_keys = allowed_keys

    def _filter(self, request: ModelRequest) -> ModelRequest:
        context = _context(request.runtime.context)
        visible = []
        for tool in request.tools:
            try:
                managed = self.catalog.resolve(tool.name)
            except LookupError:
                continue
            if managed.key in self.allowed_keys and self.policy.visible(context, managed):
                visible.append(tool)
        return request.override(tools=visible)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._filter(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._filter(request))

    def _authorize(self, request: Any) -> tuple[ExecutionContext, Any, Any, str]:
        context = _context(request.runtime.context)
        tool_call = request.tool_call
        tool_id = str(tool_call["name"])
        arguments = dict(tool_call.get("args", {}))
        arguments_hash = canonical_arguments_hash(arguments)
        try:
            managed = self.catalog.resolve(tool_id)
        except LookupError:
            return context, None, None, arguments_hash
        if managed.key not in self.allowed_keys:
            return context, managed, None, arguments_hash
        directive_denial = self._directive_denial(request.state, managed.tool, arguments)
        decision = (
            ToolDecision(
                effect=ToolDecisionType.DENY,
                reason=directive_denial,
                policy_version=self.policy.version,
            )
            if directive_denial is not None
            else self.policy.evaluate(context, managed.governance, arguments)
        )
        trace_tool_authorization(
            tool_id=tool_id,
            effect=decision.effect.value,
            arguments_hash=arguments_hash,
            context_metadata=context.trace_metadata(),
        )
        event = {
            ToolDecisionType.ALLOW: AuditEventType.TOOL_ALLOWED,
            ToolDecisionType.DENY: AuditEventType.TOOL_DENIED,
            ToolDecisionType.REQUIRE_APPROVAL: AuditEventType.TOOL_APPROVAL_REQUESTED,
        }[decision.effect]
        self._audit(
            context,
            managed,
            event=event,
            decision=decision.effect.value,
            arguments_hash=arguments_hash,
            tool_call_id=str(tool_call.get("id", "")) or None,
        )
        return context, managed, decision, arguments_hash

    @staticmethod
    def _directive_denial(state: Any, tool: Any, arguments: dict[str, Any]) -> str | None:
        """Prevent a model from bypassing an incomplete or mismatched directive.

        Visibility filtering guides the model, but execution safety cannot rely
        on model compliance.  The latest user directive is therefore checked a
        second time immediately before the Tool body runs.
        """

        messages = state.get("messages", ()) if isinstance(state, Mapping) else ()
        latest_user_entry = next(
            (
                (index, message)
                for index, message in reversed(tuple(enumerate(messages)))
                if isinstance(message, HumanMessage) and isinstance(message.content, str)
            ),
            None,
        )
        if latest_user_entry is None:
            return None
        latest_user_index, latest_user = latest_user_entry
        directive = parse_invocation_directive(latest_user.content)
        if directive is None:
            return None
        if any(isinstance(message, ToolMessage) for message in messages[latest_user_index + 1 :]):
            return "the explicit directive already produced a Tool result"
        if directive.kind is not InvocationKind.TOOL:
            return "the current user directive does not authorize a Tool call"
        if directive.resource_id != tool.name:
            return "the model-selected Tool does not match the explicit user directive"

        expected = assess_tool_slots(tool, directive)
        if (
            not directive.payload
            or directive.parse_error
            or (directive.arguments is not None and not expected.complete)
        ):
            return "the explicit Tool directive has missing or invalid required slots"
        if directive.arguments is None:
            # Natural-language payloads require semantic extraction by the Agent;
            # BaseTool performs the final schema validation on extracted values.
            return None
        actual = assess_tool_slots(
            tool,
            InvocationDirective(
                kind=InvocationKind.TOOL,
                resource_id=tool.name,
                payload="{}",
                arguments=arguments,
            ),
        )
        if not actual.complete or actual.arguments != expected.arguments:
            return "Tool arguments differ from the schema-validated explicit directive"
        return None

    def _audit(
        self,
        context: ExecutionContext,
        managed: Any,
        *,
        event: AuditEventType,
        decision: str,
        arguments_hash: str,
        tool_call_id: str | None,
    ) -> None:
        self.audit.append(
            AuditRecord(
                event_type=event,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                conversation_id=context.conversation_id,
                turn_id=context.turn_id,
                run_id=context.run_id,
                tool_call_id=tool_call_id,
                resource_id=managed.governance.tool_id,
                resource_version=managed.governance.version,
                action=managed.governance.side_effect.value,
                decision=decision,
                policy_version=self.policy.version,
                payload_hash=arguments_hash,
                metadata={"risk_level": managed.governance.risk_level.value},
            )
        )

    @staticmethod
    def _denied_message(request: Any, reason: str) -> ToolMessage:
        return ToolMessage(
            content=json.dumps({"error": "tool_not_authorized", "reason": reason}),
            tool_call_id=str(request.tool_call.get("id", "unknown")),
            name=str(request.tool_call.get("name", "unknown")),
            status="error",
        )

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        context, managed, decision, arguments_hash = self._authorize(request)
        if managed is None or managed.key not in self.allowed_keys:
            return self._denied_message(request, "tool is not in the AgentProfile allowlist")
        if decision.effect is ToolDecisionType.DENY:
            return self._denied_message(request, decision.reason)
        try:
            response = handler(request)
        except Exception:
            self._audit(
                context,
                managed,
                event=AuditEventType.FINANCIAL_TOOL_FAILED,
                decision="failed",
                arguments_hash=arguments_hash,
                tool_call_id=str(request.tool_call.get("id", "")) or None,
            )
            raise
        self._audit(
            context,
            managed,
            event=AuditEventType.FINANCIAL_TOOL_EXECUTED,
            decision="executed",
            arguments_hash=arguments_hash,
            tool_call_id=str(request.tool_call.get("id", "")) or None,
        )
        return response

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        # Authorization writes a durable audit record.  Keep that synchronous
        # SQLAlchemy work off the Agent Server event loop; blockbuster treats a
        # leaked sqlite call here as a production correctness error.
        context, managed, decision, arguments_hash = await asyncio.to_thread(
            self._authorize, request
        )
        if managed is None or managed.key not in self.allowed_keys:
            return self._denied_message(request, "tool is not in the AgentProfile allowlist")
        if decision.effect is ToolDecisionType.DENY:
            return self._denied_message(request, decision.reason)
        try:
            response = await handler(request)
        except Exception:
            await asyncio.to_thread(
                self._audit,
                context,
                managed,
                event=AuditEventType.FINANCIAL_TOOL_FAILED,
                decision="failed",
                arguments_hash=arguments_hash,
                tool_call_id=str(request.tool_call.get("id", "")) or None,
            )
            raise
        await asyncio.to_thread(
            self._audit,
            context,
            managed,
            event=AuditEventType.FINANCIAL_TOOL_EXECUTED,
            decision="executed",
            arguments_hash=arguments_hash,
            tool_call_id=str(request.tool_call.get("id", "")) or None,
        )
        return response


class FullIODebugMiddleware(AgentMiddleware):
    def __init__(self, *, enabled: bool) -> None:
        super().__init__()
        self.enabled = enabled

    def _log(self, direction: str, value: Any) -> None:
        if self.enabled:
            LOGGER.debug(
                "%s=%s",
                direction,
                json.dumps(redact_sensitive(_jsonable(value)), ensure_ascii=False, sort_keys=True),
            )

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        self._log("model_request", request)
        response = handler(request)
        self._log("model_response", response)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        self._log("model_request", request)
        response = await handler(request)
        self._log("model_response", response)
        return response

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._log("tool_request", request.tool_call)
        response = handler(request)
        self._log("tool_response", response)
        return response

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        self._log("tool_request", request.tool_call)
        response = await handler(request)
        self._log("tool_response", response)
        return response
