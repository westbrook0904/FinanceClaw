"""使用 LangGraph 原生 custom 流发布所有已注册工具的调用进度。"""

import asyncio

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command

from financeclaw.kernel.tool_progress import ToolProgress
from financeclaw.shared.turns.types import digest


class ToolProgressMiddleware(AgentMiddleware):
    """位于工具治理和重试外层，一次逻辑调用只发开始与最终状态。

    根 Agent 和 Worker 由同一 Factory 装配，子图沿用原生运行流；此处不依赖
    飞书，也不发送模型生成的工具参数、结果、思考内容或异常文本。
    """

    def __init__(self, agent, tools):
        """固定发布档案和工具名集合；不占用框架用于追加工具的 tools 属性。"""
        self.agent = agent
        self.tool_names = frozenset(tools)

    def _emit(self, request, status):
        """只接受固定目录中的名称，节点命名空间区分并发子图中的相同调用 ID。"""
        name = request.tool_call["name"]
        if name not in self.tool_names:
            return
        namespace = request.runtime.config.get("configurable", {}).get("checkpoint_ns", "")
        event = ToolProgress(
            call_id=digest([namespace, request.tool_call["id"]]),
            agent=self.agent,
            tool=name,
            status=status,
        )
        request.runtime.stream_writer(event.model_dump())

    @staticmethod
    def _status(result):
        """识别普通和 Command 返回的错误回执，不解析或复制工具业务正文。"""
        messages = [result]
        if isinstance(result, Command) and isinstance(result.update, dict):
            messages = result.update.get("messages", [])
        return (
            "failed"
            if any(isinstance(item, ToolMessage) and item.status == "error" for item in messages)
            else "completed"
        )

    def wrap_tool_call(self, request, handler):
        """同步工具保留原始返回和异常，交互中断单独标记为等待。"""
        self._emit(request, "started")
        try:
            result = handler(request)
        except GraphBubbleUp:
            self._emit(request, "interrupted")
            raise
        except Exception:
            self._emit(request, "failed")
            raise
        self._emit(request, self._status(result))
        return result

    async def awrap_tool_call(self, request, handler):
        """异步工具及时发出开始事件，保留取消、原生中断与失败的执行语义。"""
        self._emit(request, "started")
        try:
            result = await handler(request)
        except asyncio.CancelledError:
            self._emit(request, "cancelled")
            raise
        except GraphBubbleUp:
            self._emit(request, "interrupted")
            raise
        except Exception:
            self._emit(request, "failed")
            raise
        self._emit(request, self._status(result))
        return result
