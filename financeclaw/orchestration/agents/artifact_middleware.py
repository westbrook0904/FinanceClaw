"""工具结果 Artifact 中间件：把超大 Tool Result offload 为 Artifact。

属于 orchestration.agents 的横切中间件之一，挂在 Agent 的工具调用外层，避免
大体积工具结果撑爆模型上下文；读取侧由 ArtifactService 按 owner/scope 隔离。

"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from financeclaw.kernel import ExecutionContext
from financeclaw.modules.artifacts import ArtifactService


class ToolResultArtifactMiddleware(AgentMiddleware):
    """把超过内联阈值的工具结果转存为 Artifact 的 Agent 中间件。

    使用场景：由 AgentFactory 在装配 Agent 时挂到工具调用链上；工具返回内容
    序列化后超过内联阈值时，原始内容写入 Artifact Store（含 hash 校验与租户
    归属），消息中仅保留截断摘要与 artifact 引用。

    Attributes:
        service: Artifact 服务，负责 offload 判定、存储写入与元数据落库。

    """

    def __init__(self, service: ArtifactService) -> None:
        """保存 Artifact 服务引用，交给后续工具调用投影使用。

        Args:
            service: 已初始化的 Artifact 服务。

        """
        super().__init__()
        self.service = service

    @staticmethod
    def _context(request: Any) -> ExecutionContext:
        """从工具调用请求中取出受信任的 ExecutionContext。

        Args:
            request: LangChain 中间件的工具调用请求对象。

        Returns:
            ExecutionContext: 运行时上下文；为映射时按模型校验后返回。

        Raises:
            TypeError: runtime.context 既非 ExecutionContext 也非映射时抛出。

        """
        value = request.runtime.context
        if isinstance(value, ExecutionContext):
            return value
        if isinstance(value, Mapping):
            return ExecutionContext.model_validate(value)
        raise TypeError("trusted ExecutionContext is required")

    def _project(self, request: Any, response: Any) -> Any:
        """把工具响应投影为有界负载：超大结果替换为 Artifact 引用与摘要。"""
        # 1. 仅处理 ToolMessage，其余响应原样放行。
        if not isinstance(response, ToolMessage):
            return response
        # 2. 请求 ArtifactService offload，超出内联阈值时写入存储并返回元数据。
        projected, metadata = self.service.offload(
            response.content,
            context=self._context(request),
            source_type="tool_result",
            source_id=str(request.tool_call.get("id") or request.tool_call.get("name")),
        )
        # 3. metadata 为 None 表示内容未超阈值，无需生成 Artifact 引用。
        if metadata is None:
            return response
        # 4. 组装 artifact 引用（含 hash 校验信息），写回响应副本返回。
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
        """在同步工具调用外层对响应做 Artifact 投影。"""
        return self._project(request, handler(request))

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        """在异步工具调用外层对响应做 Artifact 投影。

        Args:
            request: 工具调用请求对象。
            handler: 后续调用链，返回工具响应。

        Returns:
            Any: 投影后的工具响应；投影本身在线程池中执行以避免阻塞事件循环。

        """
        response = await handler(request)
        return await asyncio.to_thread(self._project, request, response)
