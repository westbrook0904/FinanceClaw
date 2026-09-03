"""Agent 中间件基座：工具治理、追踪观测、输入输出调试与脱敏工具函数。

属于 orchestration.agents 的基础模块：提供敏感信息脱敏与参数哈希等公共工具，
以及工具治理（可见性过滤、授权决策、审计、指令一致性校验）、上下文追踪与
完整输入输出调试等 LangChain Agent Middleware 实现。

"""

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

# 模型输入/输出调试日志器，development 模式下输出脱敏后的完整输入输出。
LOGGER = logging.getLogger("financeclaw.model_io")

# 敏感键名匹配正则：命中任一关键字的映射键在脱敏时整体替换为占位符。
_SENSITIVE_KEYS = re.compile(
    r"(?:authorization|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|password|dsn|credential|reasoning|thinking|hidden)",
    re.IGNORECASE,
)
# 敏感取值匹配正则：字符串值中命中的凭证形态子串替换为占位符。
_SECRET_VALUES = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk|pk|lsv2_pt)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?:postgres(?:ql)?|redis)://[^\s]+", re.IGNORECASE),
)


def canonical_arguments_hash(arguments: Mapping[str, Any]) -> str:
    """计算工具参数的规范化哈希，作为审计记录中的负载指纹。

    Args:
        arguments: 工具调用参数映射。

    Returns:
        str: 规范化 JSON（键排序、紧凑分隔符）后的 SHA-256 十六进制摘要。

    """
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode()).hexdigest()


def redact_sensitive(value: Any) -> Any:
    """递归脱敏任意结构：命中敏感键的映射值与命中凭证形态的字符串被替换。

    Args:
        value: 任意可递归遍历的结构（映射、序列、字符串或标量）。

    Returns:
        Any: 与输入同构的脱敏副本；敏感键值与凭证子串替换为 ``<redacted>``。

    """
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
    """把任意值转换为可 JSON 序列化的等价结构，兜底使用 repr。

    Args:
        value: 任意对象；Pydantic 模型走 model_dump，其余按类型递归降级。

    Returns:
        Any: JSON 安全的结构副本。

    """
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
    """把运行时上下文取值规范化为受信任的 ExecutionContext。

    Args:
        value: runtime.context 的原始取值。

    Returns:
        ExecutionContext: 已是 ExecutionContext 时原样返回；为映射时按模型
            校验后返回。

    Raises:
        TypeError: 取值既非 ExecutionContext 也非映射时抛出。

    """
    if isinstance(value, ExecutionContext):
        return value
    if isinstance(value, Mapping):
        return ExecutionContext.model_validate(value)
    raise TypeError("trusted ExecutionContext is required")


@traceable(name="tool.authorization", run_type="chain", tags=["stage:1"])
def trace_tool_authorization(
    *, tool_id: str, effect: str, arguments_hash: str, context_metadata: dict[str, str]
) -> None:
    """上报工具授权决策的观测 span。

    Args:
        tool_id: 工具标识。
        effect: 授权决策结果（allow、deny 或 require_approval）。
        arguments_hash: 工具参数的规范化哈希。
        context_metadata: 执行上下文的追踪元数据。

    """
    del tool_id, effect, arguments_hash, context_metadata


@traceable(name="context.prepare", run_type="chain", tags=["stage:1"])
def trace_context_prepare(*, message_count: int, context_metadata: dict[str, str]) -> None:
    """上报模型调用前上下文准备的观测 span。

    Args:
        message_count: 当前状态中的消息数量。
        context_metadata: 执行上下文的追踪元数据。

    """
    del message_count, context_metadata


class ContextTraceMiddleware(AgentMiddleware):
    """在每次模型调用前上报上下文准备观测 span 的轻量中间件。

    使用场景：由 AgentFactory 挂到每个 Agent 上，把消息规模与执行上下文元数据
    写入 LangSmith 追踪，便于检索排查各次模型调用的上下文状态。

    """

    def before_model(self, state: Any, runtime: Any) -> None:
        """模型调用前上报消息数量与上下文追踪元数据。

        Args:
            state: Agent 当前图状态，含消息序列。
            runtime: LangChain 运行时，携带 ExecutionContext。

        """
        context = _context(runtime.context)
        trace_context_prepare(
            message_count=len(state.get("messages", ())),
            context_metadata=context.trace_metadata(),
        )


class ToolGovernanceMiddleware(AgentMiddleware):
    """工具治理中间件：控制模型可见工具并守卫每一次工具调用。

    使用场景：由 AgentFactory 挂到每个 Agent 上；模型调用前按策略过滤可见
    工具，工具调用前完成允许集合校验、策略授权、指令一致性校验、审计记录
    与失败/执行事件上报。

    Attributes:
        catalog: 工具目录，按名称解析受治理的受管工具。
        policy: 工具策略，提供可见性与授权决策评估。
        audit: 审计仓储，记录授权与执行事件。
        allowed_keys: 本 Agent 允许的工具键集合（tool_id@version 形式的元组）。

    """

    def __init__(
        self,
        catalog: ToolCatalog,
        policy: ToolPolicy,
        audit: AuditRepository,
        *,
        allowed_keys: frozenset[tuple[str, str]],
    ) -> None:
        """保存治理依赖与允许键集合。

        Args:
            catalog: 工具目录。
            policy: 工具策略。
            audit: 审计仓储。
            allowed_keys: 允许的工具键集合。

        """
        super().__init__()
        self.catalog = catalog
        self.policy = policy
        self.audit = audit
        self.allowed_keys = allowed_keys

    def _filter(self, request: ModelRequest) -> ModelRequest:
        """按允许集合与策略可见性过滤本次暴露给模型的工具。

        Args:
            request: 模型调用请求，携带候选工具。

        Returns:
            ModelRequest: 仅保留可见工具的覆盖请求；目录外工具一律剔除。

        """
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
        """在同步模型调用外层完成工具可见性过滤。"""
        return handler(self._filter(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """在异步模型调用外层完成工具可见性过滤。"""
        return await handler(self._filter(request))

    def _authorize(self, request: Any) -> tuple[ExecutionContext, Any, Any, str]:
        """对一次工具调用完成治理授权，返回决策所需的完整信息。

        Args:
            request: 工具调用请求对象，携带 tool_call 与运行时上下文。

        Returns:
            tuple: ``(context, managed, decision, arguments_hash)``；工具不在
                目录或不在允许集合时 decision 为 None，由调用方拒绝。

        """
        # 1. 解析执行上下文、工具调用标识与参数，并计算参数哈希。
        context = _context(request.runtime.context)
        tool_call = request.tool_call
        tool_id = str(tool_call["name"])
        arguments = dict(tool_call.get("args", {}))
        arguments_hash = canonical_arguments_hash(arguments)
        try:
            managed = self.catalog.resolve(tool_id)
        except LookupError:
            return context, None, None, arguments_hash
        # 2. 工具不在允许集合时不做策略评估，交由调用方直接拒绝。
        if managed.key not in self.allowed_keys:
            return context, managed, None, arguments_hash
        # 3. 存在显式指令时校验一致性：不一致则直接构造拒绝决策。
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
        # 4. 上报授权观测 span。
        trace_tool_authorization(
            tool_id=tool_id,
            effect=decision.effect.value,
            arguments_hash=arguments_hash,
            context_metadata=context.trace_metadata(),
        )
        # 5. 把授权决策映射为审计事件并落库。
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
        """校验本次工具调用与用户显式指令的一致性，返回拒绝理由或 None。

        Args:
            state: Agent 图状态（映射），用于定位最新用户消息与消息序列。
            tool: 被调用工具的 BaseTool 实例。
            arguments: 模型实际产出的调用参数。

        Returns:
            str | None: 与指令冲突时的拒绝理由；无指令或完全一致时返回 None。

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
        # 1. 找不到可解析指令的最新用户消息时放行。
        if latest_user_entry is None:
            return None
        latest_user_index, latest_user = latest_user_entry
        directive = parse_invocation_directive(latest_user.content)
        if directive is None:
            return None
        # 2. 指令已产生过工具结果时，禁止再次借同一指令调用工具。
        if any(isinstance(message, ToolMessage) for message in messages[latest_user_index + 1 :]):
            return "the explicit directive already produced a Tool result"
        # 3. 被调用工具必须与指令指向的能力一致，否则拒绝。
        expected_tool_name = (
            directive.resource_id
            if directive.kind is InvocationKind.TOOL
            else f"delegate_{directive.kind.value}__{directive.resource_id}"
        )
        if expected_tool_name != tool.name:
            return "the model-selected capability does not match the explicit user directive"
        # 4. 指令槽位缺失、解析失败或校验不通过时拒绝。
        expected = assess_tool_slots(tool, directive)
        if (
            not directive.payload
            or directive.parse_error
            or (directive.arguments is not None and not expected.complete)
        ):
            return "the explicit Tool directive has missing or invalid required slots"
        if directive.arguments is None:
            return None
        # 5. 实际参数必须与指令参数经 schema 校验后的结果完全一致。
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
        """把一条工具相关审计事件写入审计仓储。

        Args:
            context: 执行上下文，提供租户、主体与轮次标识。
            managed: 受管工具，提供治理元数据。
            event: 审计事件类型。
            decision: 决策结果文本（如 allow、deny、executed）。
            arguments_hash: 工具参数的规范化哈希。
            tool_call_id: 工具调用标识，缺失时为 None。

        """
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
        """构造工具被拒绝时的错误 ToolMessage，回填给模型消费。

        Args:
            request: 工具调用请求对象，用于取回调用标识与工具名。
            reason: 拒绝理由。

        Returns:
            ToolMessage: status 为 error 的拒绝消息。

        """
        return ToolMessage(
            content=json.dumps({"error": "tool_not_authorized", "reason": reason}),
            tool_call_id=str(request.tool_call.get("id", "unknown")),
            name=str(request.tool_call.get("name", "unknown")),
            status="error",
        )

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """在同步工具调用外层完成授权守卫与审计上报。

        Args:
            request: 工具调用请求对象。
            handler: 后续调用链，执行真实工具。

        Returns:
            Any: 工具响应；未授权或被拒绝时返回错误 ToolMessage。

        """
        # 1. 完成治理授权，拿到受管工具与决策结果。
        context, managed, decision, arguments_hash = self._authorize(request)
        # 2. 不在目录或不在允许集合内时直接拒绝。
        if managed is None or managed.key not in self.allowed_keys:
            return self._denied_message(request, "tool is not in the AgentProfile allowlist")
        if decision.effect is ToolDecisionType.DENY:
            return self._denied_message(request, decision.reason)
        try:
            # 3. 执行真实工具；失败时记录失败审计事件后原样抛出。
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
        # 4. 执行成功时记录执行审计事件并返回响应。
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
        """异步版本：授权与审计落库在线程池中执行，行为与同步版本一致。

        Args:
            request: 工具调用请求对象。
            handler: 后续调用链，执行真实工具。

        Returns:
            Any: 工具响应；未授权或被拒绝时返回错误 ToolMessage。

        """
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
    """完整输入输出调试中间件：以调试级别记录脱敏后的模型与工具交互。

    使用场景：由 AgentFactory 在 development 模式（debug_full_io 开启）下挂到
    Agent 上，把每次模型与工具调用的请求响应写入 ``financeclaw.model_io`` 日志。

    Attributes:
        enabled: 是否启用调试日志；关闭时所有记录调用为空操作。

    """

    def __init__(self, *, enabled: bool) -> None:
        """保存启用开关。

        Args:
            enabled: 是否启用调试日志。

        """
        super().__init__()
        self.enabled = enabled

    def _log(self, direction: str, value: Any) -> None:
        """以 JSON 形式记录一条脱敏后的调试日志。

        Args:
            direction: 日志方向标识（如 model_request、tool_response）。
            value: 待记录的对象，先转 JSON 安全结构再脱敏。

        """
        if self.enabled:
            LOGGER.debug(
                "%s=%s",
                direction,
                json.dumps(redact_sensitive(_jsonable(value)), ensure_ascii=False, sort_keys=True),
            )

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelResponse:
        """在同步模型调用外层记录请求与响应。"""
        self._log("model_request", request)
        response = handler(request)
        self._log("model_response", response)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """在异步模型调用外层记录请求与响应。"""
        self._log("model_request", request)
        response = await handler(request)
        self._log("model_response", response)
        return response

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """在同步工具调用外层记录请求与响应。"""
        self._log("tool_request", request.tool_call)
        response = handler(request)
        self._log("tool_response", response)
        return response

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """在异步工具调用外层记录请求与响应。"""
        self._log("tool_request", request.tool_call)
        response = await handler(request)
        self._log("tool_response", response)
        return response
