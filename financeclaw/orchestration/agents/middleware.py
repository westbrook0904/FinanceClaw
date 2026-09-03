"""实现上下文跟踪、工具治理和受控完整输入输出日志。"""

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

from financeclaw.kernel import ExecutionContext
from financeclaw.modules.audit import AuditEventType, AuditRecord, AuditRepository
from financeclaw.orchestration.tools import ToolCatalog, ToolDecision, ToolDecisionType, ToolPolicy

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
    """对规范化内容计算稳定 SHA-256，供幂等、审批绑定或审计使用。"""
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode()).hexdigest()


def redact_sensitive(value: Any) -> Any:
    """递归遍历映射和序列，按键名遮盖令牌、密钥和授权信息。"""
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
    """把 Pydantic、映射和序列递归转换为 JSON 可序列化结构。"""
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
    """从 LangChain 请求或工具运行时提取并校验可信执行上下文。"""
    if isinstance(value, ExecutionContext):
        return value
    if isinstance(value, Mapping):
        return ExecutionContext.model_validate(value)
    raise TypeError("trusted ExecutionContext is required")


@traceable(name="tool.authorization", run_type="chain", tags=["stage:1"])
def trace_tool_authorization(
    *, tool_id: str, effect: str, arguments_hash: str, context_metadata: dict[str, str]
) -> None:
    """创建工具授权追踪节点，并返回策略决定供调用链记录。"""
    del tool_id, effect, arguments_hash, context_metadata


@traceable(name="context.prepare", run_type="chain", tags=["stage:1"])
def trace_context_prepare(*, message_count: int, context_metadata: dict[str, str]) -> None:
    """为模型上下文准备阶段创建可观测性追踪节点。"""
    del message_count, context_metadata


class ContextTraceMiddleware(AgentMiddleware):
    """定义上下文TraceMiddleware。

    适用场景：
        用于 Agent 模型或工具调用进入下一处理器前后的横切治理场景。
    """

    def before_model(self, state: Any, runtime: Any) -> None:
        """把可信执行上下文的脱敏元数据写入当前追踪跨度。"""
        context = _context(runtime.context)
        trace_context_prepare(
            message_count=len(state.get("messages", ())),
            context_metadata=context.trace_metadata(),
        )


class ToolGovernanceMiddleware(AgentMiddleware):
    """在工具可见性与执行两个阶段强制实施授权、审批和审计。

    适用场景：
        用于 Agent 模型或工具调用进入下一处理器前后的横切治理场景。

    属性：
        catalog: 用于解析固定版本目标的只读目录。
        policy: 在副作用执行前作出确定性授权或记忆处理决定的策略。
        audit: 记录授权、执行和状态变化的审计仓储。
        allowed_keys: 当前配置明确允许的值集合。
    """

    def __init__(
        self,
        catalog: ToolCatalog,
        policy: ToolPolicy,
        audit: AuditRepository,
        *,
        allowed_keys: frozenset[tuple[str, str]],
    ) -> None:
        """注入并保存工具GovernanceMiddleware所需的协作对象，同时校验构造期不变量。"""
        super().__init__()
        self.catalog = catalog
        self.policy = policy
        self.audit = audit
        self.allowed_keys = allowed_keys

    def _filter(self, request: ModelRequest) -> ModelRequest:
        """按当前身份与 Agent 白名单过滤模型可见工具，并记录过滤结果。"""
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
        """在同步模型调用前后应用该中间件职责。"""
        return handler(self._filter(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """在异步模型调用前后应用该中间件职责。"""
        return await handler(self._filter(request))

    def _authorize(self, request: Any) -> tuple[ExecutionContext, Any, Any, str]:
        """提取执行上下文与规范化参数，解析工具并完成策略授权。"""
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
        """构造显式调用被拒绝时的模型请求，使模型只能解释拒绝原因。"""
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
        expected_tool_name = (
            directive.resource_id
            if directive.kind is InvocationKind.TOOL
            else f"delegate_{directive.kind.value}__{directive.resource_id}"
        )
        if expected_tool_name != tool.name:
            return "the model-selected capability does not match the explicit user directive"

        expected = assess_tool_slots(tool, directive)
        if (
            not directive.payload
            or directive.parse_error
            or (directive.arguments is not None and not expected.complete)
        ):
            return "the explicit Tool directive has missing or invalid required slots"
        if directive.arguments is None:
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
        """构造不可变审计事件并写入审计仓储。"""
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
        """生成不泄露策略内部细节的稳定工具拒绝消息。"""
        return ToolMessage(
            content=json.dumps({"error": "tool_not_authorized", "reason": reason}),
            tool_call_id=str(request.tool_call.get("id", "unknown")),
            name=str(request.tool_call.get("name", "unknown")),
            status="error",
        )

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """在同步工具调用前授权并记录决定，调用后记录成功或失败。"""
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
        """在异步工具调用前授权并记录决定，调用后记录成功或失败。"""
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
    """仅在显式启用时记录脱敏后的模型与工具完整输入输出。

    适用场景：
        用于 Agent 模型或工具调用进入下一处理器前后的横切治理场景。

    属性：
        enabled: 是否启用该可选能力。
    """

    def __init__(self, *, enabled: bool) -> None:
        """注入并保存FullIODebugMiddleware所需的协作对象，同时校验构造期不变量。"""
        super().__init__()
        self.enabled = enabled

    def _log(self, direction: str, value: Any) -> None:
        """在调试开关启用时记录方向和递归脱敏后的完整载荷。"""
        if self.enabled:
            LOGGER.debug(
                "%s=%s",
                direction,
                json.dumps(redact_sensitive(_jsonable(value)), ensure_ascii=False, sort_keys=True),
            )

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        """在同步模型调用前后应用该中间件职责。"""
        self._log("model_request", request)
        response = handler(request)
        self._log("model_response", response)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """在异步模型调用前后应用该中间件职责。"""
        self._log("model_request", request)
        response = await handler(request)
        self._log("model_response", response)
        return response

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """在同步工具调用前后应用该中间件职责。"""
        self._log("tool_request", request.tool_call)
        response = handler(request)
        self._log("tool_response", response)
        return response

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """在异步工具调用前后应用该中间件职责。"""
        self._log("tool_request", request.tool_call)
        response = await handler(request)
        self._log("tool_response", response)
        return response
