"""Alembic environment for the FinanceClaw application database only."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Alembic discovers tables through imports registered on the shared metadata.
from financeclaw.audit import tables as _audit_tables  # noqa: F401
from financeclaw.conversation import tables as _conversation_tables  # noqa: F401
from financeclaw.infrastructure import FinanceClawSettings, normalize_database_url
from financeclaw.infrastructure.database import ensure_database_parent
from financeclaw.infrastructure.orm import Base
from financeclaw.workflows import tables as _workflow_tables  # noqa: F401

config = context.config
if config.config_file_name is not None:
    # Migration setup must not disable application loggers in an embedding
    # process (for example pytest or an administrative worker).
    fileConfig(config.config_file_name, disable_existing_loggers=False)
settings = FinanceClawSettings()
database_url = normalize_database_url(settings.database_url.get_secret_value())
ensure_database_parent(database_url)
config.set_main_option("sqlalchemy.url", database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
