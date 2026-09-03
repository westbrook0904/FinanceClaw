"""对外接口适配，将传输协议转换为应用层请求与响应。"""

from .app import create_app, create_default_app

__all__ = ["create_app", "create_default_app"]
