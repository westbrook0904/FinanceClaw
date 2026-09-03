"""HTTP 接口子包：FastAPI 应用装配与协议适配（认证、错误映射、SSE 输出）。

对上暴露 ``create_app`` 与 ``create_default_app`` 两个工厂，前者用于
注入式装配（测试与定制），后者按配置全量装配（生产启动入口）。
"""

from .app import create_app, create_default_app

__all__ = ["create_app", "create_default_app"]
