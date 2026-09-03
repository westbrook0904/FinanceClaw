"""在工具返回模型前自动外置过大的结果载荷。"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from financeclaw.kernel import ExecutionContext
from financeclaw.modules.artifacts import ArtifactService


class ToolResultArtifactMiddleware(AgentMiddleware):
    """将超出内联阈值的工具结果替换为持久化制品引用。

    适用场景：
        用于 Agent 模型或工具调用进入下一处理器前后的横切治理场景。

    属性：
        service: 执行该适配层所依赖的领域或应用服务。
    """

    def __init__(self, service: ArtifactService) -> None:
        """注入并保存工具结果制品Middleware所需的协作对象，同时校验构造期不变量。"""
        super().__init__()
        self.service = service

    @staticmethod
    def _context(request: Any) -> ExecutionContext:
        """从 LangChain 请求或工具运行时提取并校验可信执行上下文。"""
        value = request.runtime.context
        if isinstance(value, ExecutionContext):
            return value
        if isinstance(value, Mapping):
            return ExecutionContext.model_validate(value)
        raise TypeError("trusted ExecutionContext is required")

    def _project(self, request: Any, response: Any) -> Any:
        """把工具响应交给制品服务，必要时替换为轻量引用。"""
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
        """在同步工具调用前后应用该中间件职责。"""
        return self._project(request, handler(request))

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """在异步工具调用前后应用该中间件职责。"""
        response = await handler(request)
        return await asyncio.to_thread(self._project, request, response)
