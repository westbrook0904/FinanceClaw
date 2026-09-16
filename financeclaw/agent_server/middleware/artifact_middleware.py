"""统一接收工具结果，补来源并在进入模型循环前外置大正文。"""

import asyncio
import json
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from financeclaw.agent_server.context.artifacts import SOURCE_KEY, ToolResultArchive
from financeclaw.agent_server.context.turns import trusted_context
from financeclaw.kernel.turns import message_source
from financeclaw.shared.artifacts.service import ArtifactService
from financeclaw.shared.artifacts.views import READ_BYTES, READ_KEY, VIEW_KEY, encode


class ToolResultArtifactMiddleware(AgentMiddleware):
    """不要求外部工具标注；平台声明的结构化控制回执保留完整正文。"""

    def __init__(
        self,
        service: ArtifactService,
        *,
        protected_tools: frozenset[str] = frozenset(),
        mcp_views=None,
        reader_tools: frozenset[str] = frozenset(),
    ):
        """配置统一归档阈值和平台声明的结构保护工具。"""
        self.service = service
        self.archive = ToolResultArchive(service)
        self.protected_tools = protected_tools
        self.mcp_views = mcp_views or {}
        self.reader_tools = reader_tools

    def _project(self, request: Any, response: Any) -> Any:
        """保留 Command 更新结构，对其中每条工具结果应用归档规则。"""
        if isinstance(response, Command) and isinstance(response.update, dict):
            update = dict(response.update)
            if "messages" in update:
                update["messages"] = [
                    self._project(request, item)
                    if isinstance(item, ToolMessage)
                    and item.tool_call_id == request.tool_call["id"]
                    else item
                    for item in update["messages"]
                ]
            return Command(
                graph=response.graph, update=update, resume=response.resume, goto=response.goto
            )
        if not isinstance(response, ToolMessage):
            return response
        context = trusted_context(request.runtime)
        metadata = {**response.additional_kwargs, SOURCE_KEY: message_source(context)}
        # Only platform-owned control tools can grant full-structure protection.
        name = request.tool_call.get("name")
        protected = name in self.protected_tools
        if name in self.reader_tools and response.status == "success":
            if len(str(response.content).encode()) > READ_BYTES:
                raise ValueError("artifact read response exceeds its byte budget")
            # 仅发布目录内的平台读取器可保留原文件引用；内容已有独立字节边界。
            return response.model_copy(update={"additional_kwargs": metadata})
        for key in ("artifact_ref", READ_KEY, VIEW_KEY):
            metadata.pop(key, None)
        if name in self.mcp_views:
            metadata[VIEW_KEY] = {"mcp": True, "rule": self.mcp_views[name]}
        metadata["preserve_structure"] = protected
        message = response.model_copy(update={"additional_kwargs": metadata})
        payload = json.dumps(
            {"content": message.content, "artifact": message.artifact},
            ensure_ascii=False,
            default=str,
        )
        if protected:
            if len(payload.encode()) > self.service.inline_bytes:
                raise ValueError(
                    "protected structured result exceeds inline budget; narrow the task"
                )
            return message
        force_reference = self.mcp_views.get(name, {}).get("delivery") == "reference"
        if len(encode(message.content).encode()) <= self.service.inline_bytes and not (
            force_reference and response.status == "success"
        ):
            return message
        return self.archive.project(message, context)

    def wrap_tool_call(self, request: Any, handler: Callable) -> Any:
        """同步工具返回后统一处理。"""
        return self._project(request, handler(request))

    async def awrap_tool_call(self, request: Any, handler: Callable) -> Any:
        """异步工具完成后在线程中执行持久化 I/O。"""
        response = await handler(request)
        return await asyncio.to_thread(self._project, request, response)
