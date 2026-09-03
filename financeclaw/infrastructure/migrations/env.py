"""配置 Alembic 离线 SQL 生成与在线数据库迁移。"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from financeclaw.infrastructure import FinanceClawSettings, normalize_database_url
from financeclaw.infrastructure.database import ensure_database_parent
from financeclaw.infrastructure.orm import Base

from financeclaw.modules.audit import tables as _audit_tables  # noqa: F401
from financeclaw.modules.conversation import tables as _conversation_tables  # noqa: F401
from financeclaw.modules.outbox import tables as _outbox_tables  # noqa: F401
from financeclaw.modules.workflows import tables as _workflow_tables  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
settings = FinanceClawSettings()
database_url = normalize_database_url(settings.database_url.get_secret_value())
ensure_database_parent(database_url)
config.set_main_option("sqlalchemy.url", database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """不连接数据库，根据 URL 和元数据生成迁移 SQL。"""
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
    """建立数据库连接，在事务中执行待应用的 Alembic 迁移。"""
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
