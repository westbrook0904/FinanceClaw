"""调用偏好指令中间件：解析用户消息中的 ``/tool``、``/workflow``、``/agent`` 指令。

属于 orchestration.agents 的模型调用中间件：在每次模型调用前解析最新用户消息
中的显式调用偏好指令，把匹配的候选工具收敛为唯一能力并注入执行指令；指令表达
的是偏好而非身份或权限，权限仍由工具治理与 AgentProfile 决定。

"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from .directives import InvocationKind, assess_tool_slots, parse_invocation_directive

# 注入系统提示的指令区域包裹前缀，便于与既有提示内容分隔并识别。
_REGION_PREFIX = "\n\n<financeclaw_invocation_directive>\n"
# 注入系统提示的指令区域包裹后缀。
_REGION_SUFFIX = "\n</financeclaw_invocation_directive>"


class InvocationDirectiveMiddleware(AgentMiddleware):
    """把用户显式调用偏好指令转译为模型执行指令的中间件。

    使用场景：由 AgentFactory 默认挂到每个 Agent 上；当最新用户消息匹配
    ``/tool``、``/workflow`` 或 ``/agent`` 指令时，收敛工具候选、注入针对性
    指令（直接调用、追问缺失槽位或声明能力不可用），否则原样放行。

    """

    def _apply(self, request: ModelRequest) -> ModelRequest:
        """解析最新用户消息中的指令，并按槽位评估结果改写模型请求。"""
        # 1. 仅当最新消息是字符串内容的用户消息时才尝试解析指令。
        if not request.messages or not isinstance(request.messages[-1], HumanMessage):
            return request
        content = request.messages[-1].content
        if not isinstance(content, str):
            return request
        directive = parse_invocation_directive(content)
        # 2. 无指令时原样放行，不做任何改写。
        if directive is None:
            return request
        # 3. 计算指令对应的能力名：tool 用原 id，workflow/agent 用委托包装名。
        capability_name = (
            directive.resource_id
            if directive.kind is InvocationKind.TOOL
            else f"delegate_{directive.kind.value}__{directive.resource_id}"
        )
        # 4. 在候选工具中查找匹配能力；找不到时注入"不可用"指令并清空工具。
        selected = self._find_tool(request.tools, capability_name)
        if selected is None:
            instruction = (
                f"The user requested {directive.kind.value} '{directive.resource_id}', but the "
                "matching capability is unknown or not visible under the current policy. Explain "
                "that it cannot be used; do not silently choose another capability."
            )
            return self._override(request, instruction=instruction, tools=[])
        # 5. 依据工具 schema 评估指令槽位是否齐备且合法。
        assessment = assess_tool_slots(selected, directive)
        # 6. 参数已给定且校验通过：注入"仅调用该能力、参数逐字使用"的指令。
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
        # 7. 参数以自然语言给出：要求模型从中抽取参数，缺失字段须追问。
        if directive.payload and not directive.payload.startswith("{"):
            instruction = (
                f"The user explicitly prefers {directive.kind.value} '{directive.resource_id}'. "
                "Extract its arguments from the user's natural-language payload. Call only the "
                "matching capability if every required schema field is known; otherwise ask one "
                "concise clarification for the missing fields."
            )
            return self._override(request, instruction=instruction, tools=[selected])
        # 8. 参数缺失或校验失败：禁止本回合调用工具，要求模型仅追问缺失槽位。
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
        """按名称在候选工具中查找 BaseTool 形式的匹配项。

        Args:
            tools: 本次暴露给模型的工具候选（BaseTool 或 OpenAI 风格字典）。
            name: 目标能力名称。

        Returns:
            BaseTool | None: 匹配的 BaseTool；未命中时返回 None。

        """
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
        """把指令追加到系统提示的指令区域，并覆盖工具候选与工具选择。

        Args:
            request: 当前模型调用请求。
            instruction: 注入指令区域的英文执行指令文本。
            tools: 覆盖后的工具候选列表。
            tool_choice: 覆盖后的工具选择策略，默认 None 表示不改写。

        Returns:
            ModelRequest: 改写系统提示与工具后的新请求；既有附加参数原样保留。

        """
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
        """在同步模型调用外层应用调用偏好指令改写。"""
        return handler(self._apply(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """在异步模型调用外层应用调用偏好指令改写。"""
        return await handler(self._apply(request))
