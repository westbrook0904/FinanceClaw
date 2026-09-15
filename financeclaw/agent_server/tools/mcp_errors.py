"""MCP 的可公开错误与统一重试回调，不携带认证头或上游异常正文。"""

from financeclaw.agent_server.tools.policy import TransientToolError


class MCPError(RuntimeError):
    """不可重试的协议错误，直接作为当前工具调用的失败回执。"""


class MCPUnavailableError(TransientToolError):
    """通用 MCP 的瞬态失败，由现有只读重试层计次执行。"""


def tool_retry_failure(error: Exception) -> str:
    """仅将 MCP 重试耗尽转为原生错误 ToolMessage，其他工具保持原有语义。"""
    if isinstance(error, MCPUnavailableError):
        return "MCP_UNAVAILABLE: MCP 服务重试后仍不可用，本次未取得查询结果。"
    raise error
