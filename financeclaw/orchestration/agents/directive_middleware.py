"""识别显式调用指令，限制该轮模型可见的工具集合。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from .directives import InvocationKind, assess_tool_slots, parse_invocation_directive

_REGION_PREFIX = "\n\n<financeclaw_invocation_directive>\n"
_REGION_SUFFIX = "\n</financeclaw_invocation_directive>"


class InvocationDirectiveMiddleware(AgentMiddleware):
    """将显式调用请求约束为单个目标工具，减少模型路由歧义。

    适用场景：
        用于 Agent 模型或工具调用进入下一处理器前后的横切治理场景。
    """

    def _apply(self, request: ModelRequest) -> ModelRequest:
        """解析最新用户指令；目标明确且参数完整时仅暴露对应工具。"""
        if not request.messages or not isinstance(request.messages[-1], HumanMessage):
            return request
        content = request.messages[-1].content
        if not isinstance(content, str):
            return request
        directive = parse_invocation_directive(content)
        if directive is None:
            return request

        capability_name = (
            directive.resource_id
            if directive.kind is InvocationKind.TOOL
            else f"delegate_{directive.kind.value}__{directive.resource_id}"
        )
        selected = self._find_tool(request.tools, capability_name)
        if selected is None:
            instruction = (
                f"The user requested {directive.kind.value} '{directive.resource_id}', but the "
                "matching capability is unknown or not visible under the current policy. Explain "
                "that it cannot be used; do not silently choose another capability."
            )
            return self._override(request, instruction=instruction, tools=[])

        assessment = assess_tool_slots(selected, directive)
        if directive.arguments is not None and assessment.complete:
            normalized_arguments = json.dumps(
                assessment.arguments, ensure_ascii=False, sort_keys=True
            )
            instruction = (
                f"Call only {directive.kind.value} capability '{capability_name}' now with these "
                "schema-validated "
                f"arguments: {normalized_arguments}. "
                "Do not infer, add, or replace arguments."
            )
            return self._override(
                request,
                instruction=instruction,
                tools=[selected],
            )
        if directive.payload and not directive.payload.startswith("{"):
            instruction = (
                f"The user explicitly prefers {directive.kind.value} '{directive.resource_id}'. "
                "Extract its arguments from the user's natural-language payload. Call only the "
                "matching capability if every required schema field is known; otherwise ask one "
                "concise clarification for the missing fields."
            )
            return self._override(request, instruction=instruction, tools=[selected])

        problems = [*assessment.missing_fields, *assessment.validation_errors]
        detail = ", ".join(problems) if problems else "tool arguments"
        instruction = (
            f"The user explicitly prefers {directive.kind.value} '{directive.resource_id}', but "
            "these slots are missing "
            f"or invalid: {detail}. Ask one concise clarification that requests only those values. "
            "Do not call any Tool in this model turn."
        )
        return self._override(request, instruction=instruction, tools=[])

    @staticmethod
    def _find_tool(tools: list[BaseTool | dict[str, Any]], name: str) -> BaseTool | None:
        """查找匹配的调用DirectiveMiddleware；没有匹配项时返回空值。"""
        for tool in tools:
            if isinstance(tool, BaseTool) and tool.name == name:
                return tool
        return None

    @staticmethod
    def _override(
        request: ModelRequest,
        *,
        instruction: str,
        tools: list[BaseTool],
        tool_choice: str | None = None,
    ) -> ModelRequest:
        """复制模型请求并仅替换指定字段，兼容不同 LangChain 请求实现。"""
        existing = request.system_message
        existing_content = ""
        additional: dict[str, Any] = {}
        if existing is not None:
            existing_content = (
                existing.content
                if isinstance(existing.content, str)
                else json.dumps(existing.content, ensure_ascii=False, default=str)
            )
            additional.update(existing.additional_kwargs)
        system_message = SystemMessage(
            content=f"{existing_content}{_REGION_PREFIX}{instruction}{_REGION_SUFFIX}",
            additional_kwargs=additional,
        )
        return request.override(
            system_message=system_message,
            tools=tools,
            tool_choice=tool_choice,
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """在同步模型调用前后应用该中间件职责。"""
        return handler(self._apply(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """在异步模型调用前后应用该中间件职责。"""
        return await handler(self._apply(request))
