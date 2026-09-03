"""出站客户端适配集合：实现应用层定义的对外通信 Port。

本包属于 infrastructure 层，封装对内部 Agent Server 等目标的真实访问，
由 bootstrap.py 组合根装配后注入应用层服务。
"""

from .agent_server import LangGraphAgentServerClient

__all__ = ["LangGraphAgentServerClient"]
