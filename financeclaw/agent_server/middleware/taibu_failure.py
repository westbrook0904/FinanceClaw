"""太卜重试耗尽后给根模型可读错误，保留其他工具原有失败行为。"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from financeclaw.agent_server.tools.policy import TransientToolError
from financeclaw.kernel.taibu import TAIBU_TOOL_INPUTS


class TaibuFailureMiddleware(AgentMiddleware):
    """位于统一工具重试外层；每次真实尝试仍经过持久预算。"""

    def _failed(self, request, error):
        """只承接两个固定工具的瞬态错误，不吞掉取消或执行治理错误。"""
        if request.tool_call["name"] not in TAIBU_TOOL_INPUTS:
            raise error
        return ToolMessage(
            content="TAIBU_UNAVAILABLE: 太卜服务重试后仍不可用，本次未取得计算结果。",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    def wrap_tool_call(self, request, handler):
        """同步工具重试耗尽后返回明确的失败消息。"""
        try:
            return handler(request)
        except TransientToolError as error:
            return self._failed(request, error)

    async def awrap_tool_call(self, request, handler):
        """异步工具重试耗尽后允许根图继续处理其他工作。"""
        try:
            return await handler(request)
        except TransientToolError as error:
            return self._failed(request, error)
