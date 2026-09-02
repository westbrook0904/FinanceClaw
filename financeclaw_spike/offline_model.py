"""Deterministic tool-calling model used only by local Agent Server smoke tests."""

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import PrivateAttr


class OfflineSpikeModel(BaseChatModel):
    """Choose a demo tool from the last user message, then summarize its result."""

    _bound_tool_names: set[str] = PrivateAttr(default_factory=set)

    @property
    def _llm_type(self) -> str:
        return "financeclaw-stage0-offline"

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
            message = AIMessage(content=f"Tool result: {last.content}")
        else:
            content = str(last.content).lower()
            if "write" in content or "watchlist" in content:
                if "write_watchlist" not in self._bound_tool_names:
                    message = AIMessage(content="WRITE tool is not authorized for this run.")
                else:
                    message = AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "write_watchlist",
                                "args": {"symbol": "AAPL", "note": "stage0"},
                                "id": "stage0-write-call",
                                "type": "tool_call",
                            }
                        ],
                    )
            else:
                message = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_market_snapshot",
                            "args": {"symbol": "AAPL"},
                            "id": "stage0-read-call",
                            "type": "tool_call",
                        }
                    ],
                )
        return ChatResult(generations=[ChatGeneration(message=message)])
