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


class ToolResultArtifactMiddleware(AgentMiddleware):
    """不要求外部工具标注；平台声明的结构化控制回执保留完整正文。"""

    def __init__(self, service: ArtifactService, *, protected_tools: frozenset[str] = frozenset()):
        """配置统一归档阈值和平台声明的结构保护工具。"""
        self.service = service
        self.archive = ToolResultArchive(service)
        self.protected_tools = protected_tools

    def _project(self, request: Any, response: Any) -> Any:
        """保留 Command 更新结构，对其中每条工具结果应用归档规则。"""
        if isinstance(response, Command) and isinstance(response.update, dict):
            update = dict(response.update)
            if "messages" in update:
                update["messages"] = [self._project(request, item) for item in update["messages"]]
            return Command(
                graph=response.graph, update=update, resume=response.resume, goto=response.goto
            )
        if not isinstance(response, ToolMessage):
            return response
        context = trusted_context(request.runtime)
        metadata = {**response.additional_kwargs, SOURCE_KEY: message_source(context)}
        # Only platform-owned control tools can grant full-structure protection.
        protected = request.tool_call.get("name") in self.protected_tools
        metadata.pop("artifact_ref", None)
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
        if len(payload.encode()) <= self.service.inline_bytes:
            return message
        return self.archive.project(message, context)

    def wrap_tool_call(self, request: Any, handler: Callable) -> Any:
        """同步工具返回后统一处理。"""
        return self._project(request, handler(request))

    async def awrap_tool_call(self, request: Any, handler: Callable) -> Any:
        """异步工具完成后在线程中执行持久化 I/O。"""
        response = await handler(request)
        return await asyncio.to_thread(self._project, request, response)
