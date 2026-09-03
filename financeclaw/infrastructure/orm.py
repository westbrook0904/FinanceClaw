"""提供领域表模型共享的 SQLAlchemy 声明基类与 UTC 时间工厂。"""

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """返回带 UTC 时区的当前时间，供 ORM 默认值统一使用。"""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """定义Base。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。
    """
