"""提供无需外部模型服务即可验证编排路径的确定性聊天模型。"""

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import PrivateAttr

from .directives import InvocationKind, parse_invocation_directive


class OfflineFinanceModel(BaseChatModel):
    """以确定性规则模拟模型工具调用，用于离线测试和冒烟验证。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        _bound_tool_names: 内部 `bound tool names` 状态或依赖，不属于公开接口。
    """

    _bound_tool_names: set[str] = PrivateAttr(default_factory=set)

    @property
    def _llm_type(self) -> str:
        """返回离线模型的稳定类型名，供 LangChain 序列化和诊断。"""
        return "financeclaw-stage1-offline"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        """记录绑定工具名称并返回支持后续调用的模型副本。"""
        del tool_choice, kwargs
        bound = self.model_copy(deep=True)
        bound._bound_tool_names = {
            tool["name"] if isinstance(tool, dict) else tool.name for tool in tools
        }
        return bound

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """根据最新用户消息和已绑定工具确定性地产生聊天结果。"""
        del stop, run_manager, kwargs
        last = messages[-1]
        if isinstance(last, ToolMessage):
            if last.name == "propose_memory":
                proposal = json.loads(str(last.content))
                draft = proposal["draft"]
                return ChatResult(
                    generations=[
                        ChatGeneration(
                            message=AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "name": "confirm_memory",
                                        "args": {"proposal_id": proposal["proposal_id"], **draft},
                                        "id": "offline-confirm-memory-call",
                                        "type": "tool_call",
                                    }
                                ],
                            )
                        )
                    ]
                )
            return ChatResult(
                generations=[
                    ChatGeneration(message=AIMessage(content=f"Tool result: {last.content}"))
                ]
            )
        raw_content = str(last.content)
        directive = parse_invocation_directive(raw_content)
        directive_capability = (
            None
            if directive is None
            else (
                directive.resource_id
                if directive.kind is InvocationKind.TOOL
                else f"delegate_{directive.kind.value}__{directive.resource_id}"
            )
        )
        if directive is not None and (directive.parse_error or not directive.payload):
            problem = directive.parse_error or "required arguments"
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content=f"Please provide the missing or invalid {problem}."
                        )
                    )
                ]
            )
        if directive is not None and directive_capability not in self._bound_tool_names:
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content=(
                                f"Requested {directive.kind.value} '{directive.resource_id}' "
                                "has no registered delegation capability."
                            )
                        )
                    )
                ]
            )
        content = raw_content.lower()
        if directive is not None and directive.arguments is not None:
            name = (
                directive.resource_id
                if directive.kind is InvocationKind.TOOL
                else f"delegate_{directive.kind.value}__{directive.resource_id}"
            )
            args = directive.arguments
        elif directive is not None and directive.kind is InvocationKind.AGENT and directive.payload:
            name = f"delegate_agent__{directive.resource_id}"
            args = {"task": directive.payload}
        elif "remember preference" in content or "记住" in content:
            name = "propose_memory"
            args = {
                "kind": "preference",
                "content": "用户偏好低波动资产",
                "evidence_message_ids": ["current"],
            }
        elif "recall memory" in content or "回忆偏好" in content:
            name = "search_memories"
            args = {"query": "低波动", "limit": 5}
        elif "watchlist" in content or "自选" in content or "write" in content:
            name = "watchlist_add"
            args = {"symbol": "AAPL", "note": "stage1"}
        elif "calculate" in content or "计算" in content:
            name = "calculate"
            args = {"operation": "add", "left": 1, "right": 2}
        elif "mcp" in content:
            name = "get_demo_quote"
            args = {"symbol": "AAPL"}
        else:
            name = "market_snapshot"
            args = {"symbol": "AAPL"}
        if name not in self._bound_tool_names:
            message = AIMessage(content=f"Tool {name} is not authorized for this run.")
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "args": args,
                        "id": f"offline-{name}-call",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])
