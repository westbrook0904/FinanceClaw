"""LangChain middleware that keeps large Tool results out of Agent state."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from financeclaw.contracts import ExecutionContext

from .service import ArtifactService


class ToolResultArtifactMiddleware(AgentMiddleware):
    def __init__(self, service: ArtifactService) -> None:
        super().__init__()
        self.service = service

    @staticmethod
    def _context(request: Any) -> ExecutionContext:
        value = request.runtime.context
        if isinstance(value, ExecutionContext):
            return value
        if isinstance(value, Mapping):
            return ExecutionContext.model_validate(value)
        raise TypeError("trusted ExecutionContext is required")

    def _project(self, request: Any, response: Any) -> Any:
        if not isinstance(response, ToolMessage):
            return response
        projected, metadata = self.service.offload(
            response.content,
            context=self._context(request),
            source_type="tool_result",
            source_id=str(request.tool_call.get("id") or request.tool_call.get("name")),
        )
        if metadata is None:
            return response
        reference = {
            "artifact_id": metadata.artifact_id,
            "content_type": metadata.content_type,
            "content_hash": metadata.content_hash,
            "size_bytes": metadata.size_bytes,
        }
        additional = {**response.additional_kwargs, "artifact_ref": reference}
        return response.model_copy(
            update={"content": projected, "artifact": reference, "additional_kwargs": additional}
        )

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return self._project(request, handler(request))

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        response = await handler(request)
        return await asyncio.to_thread(self._project, request, response)
