"""数据库适配模块：构建 SQLAlchemy 引擎、管理会话生命周期与连接准备工作。

本模块属于 infrastructure 层，为各模块的 SQLAlchemy 仓储实现提供统一的
引擎与会话工厂；开发环境可用 SQLite，生产环境使用 PostgreSQL。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.infrastructure.observability import instrument_sqlalchemy_engine
from financeclaw.infrastructure.orm import Base

# 导入各模块的表定义，将 ORM 模型注册到 Base.metadata（建表与迁移都依赖它）。
from financeclaw.modules.audit import tables as _audit_tables  # noqa: F401
from financeclaw.modules.conversation import tables as _conversation_tables  # noqa: F401
from financeclaw.modules.delegation import tables as _delegation_tables  # noqa: F401
from financeclaw.modules.outbox import tables as _outbox_tables  # noqa: F401
from financeclaw.modules.workflows import tables as _workflow_tables  # noqa: F401


class ApplicationDatabase:
    """面向应用的关系型数据库封装：持有引擎与会话工厂，屏蔽方言差异。

    使用场景：由 bootstrap.py 组合根按 ``FinanceClawSettings.database_url``
    构造并注入各模块仓储；开发期可用 ``initialize_schema()`` 自动建表，
    生产环境改用 Alembic 迁移管理 schema。

    Attributes:
        engine: SQLAlchemy 异步无关的同步引擎，已注入 OTel 插桩。
        session_factory: 会话工厂，提交后不使对象过期（``expire_on_commit=False``），
            便于响应序列化。

    """

    def __init__(self, url: str, *, statement_timeout_seconds: int = 30) -> None:
        """构建数据库引擎并完成方言相关的连接配置。

        Args:
            url: 数据库连接串，支持 ``postgresql://`` 与 ``sqlite:///`` 形式。
            statement_timeout_seconds: PostgreSQL 语句级超时（秒），SQLite 下忽略。

        """
        # 1. 规范化 URL 并确保本地 SQLite 的父目录存在。
        url = normalize_database_url(url)
        ensure_database_parent(url)
        # 2. 按方言设置连接参数：SQLite 允许跨线程复用连接，PostgreSQL 设置语句超时。
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        else:
            connect_args = {"options": f"-c statement_timeout={statement_timeout_seconds * 1_000}"}
        # 3. 创建引擎（pool_pre_ping 预检失效连接），并注入 OpenTelemetry SQL 插桩。
        self.engine: Engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        instrument_sqlalchemy_engine(self.engine)
        # 4. SQLite 默认不启用外键约束，须在每个新连接上显式打开。
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
                """在每个新建 SQLite 连接上执行 PRAGMA 打开外键约束。"""
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        # 5. 构建会话工厂：提交后对象不过期，避免响应阶段触发二次查询。
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def initialize_schema(self) -> None:
        """按 ``Base.metadata`` 自动创建全部缺失的表。

        使用场景：仅用于开发与测试（``database_auto_create_schema``）；
        生产环境必须关闭，由 Alembic 迁移统一管理 schema 演进。
        """
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """提供一个带事务边界的会话：正常退出提交，异常退出回滚。

        使用场景：仓储与服务的同步访问入口；调用方无须手动 begin/commit。

        Yields:
            处于活动事务中的 ``Session``。

        """
        with self.session_factory() as session:
            with session.begin():
                yield session

    def close(self) -> None:
        """释放引擎持有的全部连接池资源，用于优雅停机。"""
        self.engine.dispose()

    def ping(self) -> bool:
        """以 ``SELECT 1`` 探测数据库连通性，供就绪检查使用。

        Returns:
            连接可用返回 True；任何异常（连接失败、超时）返回 False。

        """
        try:
            with self.engine.connect() as connection:
                return connection.scalar(text("SELECT 1")) == 1
        except Exception:
            return False


def normalize_database_url(url: str) -> str:
    """把 ``postgresql://`` 前缀改写为 SQLAlchemy 3.x 驱动所需的 ``postgresql+psycopg://``。

    Args:
        url: 原始数据库连接串。

    Returns:
        指定 psycopg 驱动的连接串；非 PostgreSQL URL 原样返回。

    """
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def ensure_database_parent(url: str) -> None:
    """确保本地 SQLite 数据库文件的父目录存在。

    使用场景：首次启动时 ``.financeclaw`` 目录可能尚不存在，先创建以免
    SQLite 连接失败；内存库与 PostgreSQL 不需要处理。

    Args:
        url: 归一化后的数据库连接串。

    """
    # 内存库与非 SQLite 后端不涉及本地文件。
    if not url.startswith("sqlite") or ":memory:" in url:
        return
    database_path = make_url(url).database
    if database_path:
        Path(database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
