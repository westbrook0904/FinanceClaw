"""管理 SQLAlchemy 引擎、事务会话和数据库连通性。"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from financeclaw.infrastructure.observability import instrument_sqlalchemy_engine
from financeclaw.infrastructure.orm import Base

from financeclaw.modules.audit import tables as _audit_tables  # noqa: F401
from financeclaw.modules.conversation import tables as _conversation_tables  # noqa: F401
from financeclaw.modules.delegation import tables as _delegation_tables  # noqa: F401
from financeclaw.modules.outbox import tables as _outbox_tables  # noqa: F401
from financeclaw.modules.workflows import tables as _workflow_tables  # noqa: F401


class ApplicationDatabase:
    """持有数据库引擎与会话工厂，统一事务和健康检查生命周期。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        engine: SQLAlchemy 数据库引擎，持有连接池和方言配置。
        session_factory: 创建独立 SQLAlchemy 事务会话的工厂。
    """

    def __init__(self, url: str, *, statement_timeout_seconds: int = 30) -> None:
        """注入并保存ApplicationDatabase所需的协作对象，同时校验构造期不变量。"""
        url = normalize_database_url(url)
        ensure_database_parent(url)
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        else:
            connect_args = {"options": f"-c statement_timeout={statement_timeout_seconds * 1_000}"}
        self.engine: Engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        instrument_sqlalchemy_engine(self.engine)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
                """为每个新 SQLite 连接启用外键约束，保持测试与生产数据库语义一致。"""
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def initialize_schema(self) -> None:
        """在开发或测试启动路径中创建当前元数据尚不存在的表。"""
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """打开事务会话；正常退出时提交，发生异常时回滚并始终关闭连接。"""
        with self.session_factory() as session:
            with session.begin():
                yield session

    def close(self) -> None:
        """释放数据库引擎及连接池持有的资源。"""
        self.engine.dispose()

    def ping(self) -> bool:
        """执行轻量查询验证数据库连接是否可用。"""
        try:
            with self.engine.connect() as connection:
                return connection.scalar(text("SELECT 1")) == 1
        except Exception:
            return False


def normalize_database_url(url: str) -> str:
    """将输入规范化为可比较、可持久化的database 模块的数据。"""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def ensure_database_parent(url: str) -> None:
    """为文件型 SQLite URL 创建缺失的父目录；其他数据库 URL 不做处理。"""
    if not url.startswith("sqlite") or ":memory:" in url:
        return
    database_path = make_url(url).database
    if database_path:
        Path(database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
