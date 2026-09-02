"""Development prompt logging and LangSmith-safe redaction demonstration."""

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langgraph.runtime import Runtime
from langsmith import Client, traceable

from financeclaw_spike.context import SpikeContext

LOGGER = logging.getLogger("financeclaw_spike.model_io")

_SENSITIVE_KEYS = re.compile(
    r"(?:authorization|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|password|dsn)",
    re.IGNORECASE,
)
_SECRET_VALUES = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?:postgres(?:ql)?|redis)://[^\s]+", re.IGNORECASE),
)


def redact_sensitive(value: Any) -> Any:
    """Recursively remove credentials before logging or tracing."""

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


def _request_payload(request: ModelRequest) -> dict[str, Any]:
    def tool_schema(tool: Any) -> dict[str, Any]:
        schema = tool.args_schema
        if schema is None:
            return {}
        if isinstance(schema, Mapping):
            return dict(schema)
        return schema.model_json_schema()

    return {
        "system_prompt": _jsonable(getattr(request, "system_prompt", None)),
        "messages": _jsonable(getattr(request, "messages", ())),
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "schema": tool_schema(tool),
            }
            for tool in getattr(request, "tools", ())
        ],
        "response_format": _jsonable(getattr(request, "response_format", None)),
    }


class DynamicToolFilterMiddleware(AgentMiddleware):
    """Hide WRITE tools from the model when trusted runtime context disallows them."""

    def _filter(self, request: ModelRequest) -> ModelRequest:
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        allow_write = getattr(context, "allow_write", True)
        if isinstance(context, Mapping):
            allow_write = bool(context.get("allow_write", True))
        if allow_write:
            return request
        tools = [tool for tool in request.tools if tool.name != "write_watchlist"]
        return request.override(tools=tools)

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


class FullPromptDebugMiddleware(AgentMiddleware):
    """Log the final model request/response only when development debug is enabled."""

    def __init__(self, *, enabled: bool) -> None:
        super().__init__()
        self.enabled = enabled

    def _log(self, direction: str, value: Any) -> None:
        if self.enabled:
            LOGGER.debug(
                "model_%s=%s",
                direction,
                json.dumps(redact_sensitive(_jsonable(value)), ensure_ascii=False, sort_keys=True),
            )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        self._log("request", _request_payload(request))
        response = handler(request)
        self._log("response", response)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        self._log("request", _request_payload(request))
        response = await handler(request)
        self._log("response", response)
        return response

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._log("tool_request", request.tool_call)
        try:
            response = handler(request)
        except Exception as exc:
            self._log("tool_error", {"type": type(exc).__name__})
            raise
        self._log("tool_response", response)
        return response

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        self._log("tool_request", request.tool_call)
        try:
            response = await handler(request)
        except Exception as exc:
            self._log("tool_error", {"type": type(exc).__name__})
            raise
        self._log("tool_response", response)
        return response


@traceable(name="spike.context.prepare", run_type="chain", tags=["stage:0"])
def trace_context_prepare(*, message_count: int, request_id: str) -> None:
    """Emit a bounded custom child run without copying prompt content."""

    del message_count, request_id


class ContextTraceMiddleware(AgentMiddleware):
    """Add the Stage-0 custom domain step to the automatic Agent/Model/Tool tree."""

    def before_model(self, state: Any, runtime: Runtime[SpikeContext]) -> None:
        context = runtime.context
        trace_context_prepare(
            message_count=len(state.get("messages", ())),
            request_id=getattr(context, "request_id", "unknown"),
        )


def build_masked_langsmith_client() -> Client:
    """Construct a client that keeps trace shape while masking sensitive I/O."""

    return Client(hide_inputs=redact_sensitive, hide_outputs=redact_sensitive)


def context_metadata(context: SpikeContext) -> dict[str, str]:
    return {
        "environment": context.environment,
        "request_id": context.request_id,
        "stage": "0",
    }
