"""Model middleware for governed slash directives and schema-based slot filling."""

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
    """Constrain an explicit directive after normal policy visibility filtering.

    A directive is never identity, authorization, or a request to bypass the
    top-level Agent.  Complete JSON arguments are constrained to the one named tool;
    incomplete arguments remove all tools for the current model turn so the
    Agent can only elicit the missing slots.
    """

    def _apply(self, request: ModelRequest) -> ModelRequest:
        # ReAct calls the model again after a ToolMessage.  Applying the original
        # directive on that second call would force an execution loop, so only a
        # currently-last HumanMessage starts directive processing.
        if not request.messages or not isinstance(request.messages[-1], HumanMessage):
            return request
        content = request.messages[-1].content
        if not isinstance(content, str):
            return request
        directive = parse_invocation_directive(content)
        if directive is None:
            return request

        if directive.kind is not InvocationKind.TOOL:
            instruction = (
                f"The user requested {directive.kind.value} '{directive.resource_id}'. "
                "Treat this as an invocation preference, never as authorization. "
                "Do not substitute a Tool or another Agent. Use the matching registered "
                "delegation capability if one is visible; otherwise explain that it is "
                "unavailable. "
                "The root conversation remains owned by the top-level Agent."
            )
            return self._override(request, instruction=instruction, tools=[])

        selected = self._find_tool(request.tools, directive.resource_id)
        if selected is None:
            instruction = (
                f"The user requested Tool '{directive.resource_id}', but that Tool is unknown or "
                "not visible under the current policy. Explain that it cannot be used; do not "
                "silently choose a different Tool."
            )
            return self._override(request, instruction=instruction, tools=[])

        assessment = assess_tool_slots(selected, directive)
        if directive.arguments is not None and assessment.complete:
            normalized_arguments = json.dumps(
                assessment.arguments, ensure_ascii=False, sort_keys=True
            )
            instruction = (
                f"Call only Tool '{directive.resource_id}' now with these schema-validated "
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
                f"The user explicitly prefers Tool '{directive.resource_id}'. Extract its "
                "arguments "
                "from the user's natural-language payload. Call only this Tool if every required "
                "schema field is known; otherwise ask one concise clarification for the "
                "missing fields."
            )
            return self._override(request, instruction=instruction, tools=[selected])

        problems = [*assessment.missing_fields, *assessment.validation_errors]
        detail = ", ".join(problems) if problems else "tool arguments"
        instruction = (
            f"The user explicitly prefers Tool '{directive.resource_id}', but these slots are "
            "missing "
            f"or invalid: {detail}. Ask one concise clarification that requests only those values. "
            "Do not call any Tool in this model turn."
        )
        return self._override(request, instruction=instruction, tools=[])

    @staticmethod
    def _find_tool(tools: list[BaseTool | dict[str, Any]], name: str) -> BaseTool | None:
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
        return handler(self._apply(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._apply(request))
