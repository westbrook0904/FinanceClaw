"""Deterministic local model used only for tests and offline Agent Server smoke."""

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import PrivateAttr


class OfflineFinanceModel(BaseChatModel):
    _bound_tool_names: set[str] = PrivateAttr(default_factory=set)

    @property
    def _llm_type(self) -> str:
        return "financeclaw-stage1-offline"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
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
        del stop, run_manager, kwargs
        last = messages[-1]
        if isinstance(last, ToolMessage):
            return ChatResult(
                generations=[
                    ChatGeneration(message=AIMessage(content=f"Tool result: {last.content}"))
                ]
            )
        content = str(last.content).lower()
        if "watchlist" in content or "自选" in content or "write" in content:
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
