"""ORM 基础模块：提供全库统一的 SQLAlchemy 声明式基类与时间戳工具。

本模块是所有模块表模型的挂载点：各模块 ``tables`` 中定义的模型都继承
这里的 ``Base``，从而纳入同一份 metadata 供建表与 Alembic 迁移使用。
"""

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """返回带 UTC 时区的当前时间，供模型时间戳字段的默认值使用。

    Returns:
        UTC 时区的 ``datetime``，统一以 UTC 存储避免时区歧义。

    """
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """全库唯一的声明式基类，各模块 ORM 模型继承它以共享同一份 metadata。

    使用场景：各模块 ``tables`` 模块定义表模型时继承；``create_all`` 与
    ``migrations/env.py`` 均以 ``Base.metadata`` 为目标元数据。
    """

    pass
