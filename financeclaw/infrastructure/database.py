"""FinanceClaw application database setup; Agent Server tables remain separate."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

# Import table modules solely to register their rows on the shared metadata
# before create_all is used by development and tests.
from financeclaw.audit import tables as _audit_tables  # noqa: F401
from financeclaw.conversation import tables as _conversation_tables  # noqa: F401
from financeclaw.delegation import tables as _delegation_tables  # noqa: F401
from financeclaw.infrastructure.orm import Base
from financeclaw.observability import instrument_sqlalchemy_engine
from financeclaw.outbox import tables as _outbox_tables  # noqa: F401
from financeclaw.workflows import tables as _workflow_tables  # noqa: F401


class ApplicationDatabase:
    def __init__(self, url: str, *, statement_timeout_seconds: int = 30) -> None:
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
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def initialize_schema(self) -> None:
        """Development/test convenience; production uses Alembic exclusively."""

        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.session_factory() as session:
            with session.begin():
                yield session

    def close(self) -> None:
        self.engine.dispose()

    def ping(self) -> bool:
        """Run a bounded readiness query through the configured connection pool."""

        try:
            with self.engine.connect() as connection:
                return connection.scalar(text("SELECT 1")) == 1
        except Exception:
            return False


def normalize_database_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def ensure_database_parent(url: str) -> None:
    """Create only the parent directory needed by a file-backed SQLite URL."""

    if not url.startswith("sqlite") or ":memory:" in url:
        return
    database_path = make_url(url).database
    if database_path:
        Path(database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
