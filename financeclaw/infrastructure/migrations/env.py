"""Alembic 运行环境：装配数据库连接与目标 metadata，执行在线/离线迁移。

本脚本由 Alembic 在执行 ``alembic upgrade`` 等命令时加载：从
``FinanceClawSettings`` 读取数据库连接串，以 ``Base.metadata`` 为比对
目标，支持离线生成 SQL 与在线直连执行两种模式。
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from financeclaw.infrastructure import FinanceClawSettings, normalize_database_url
from financeclaw.infrastructure.database import ensure_database_parent
from financeclaw.infrastructure.orm import Base

# 导入各模块表定义，确保全部 ORM 模型注册进 Base.metadata，迁移才能感知它们。
from financeclaw.modules.audit import tables as _audit_tables  # noqa: F401
from financeclaw.modules.conversation import tables as _conversation_tables  # noqa: F401
from financeclaw.modules.outbox import tables as _outbox_tables  # noqa: F401
from financeclaw.modules.workflows import tables as _workflow_tables  # noqa: F401

config = context.config
# 加载 alembic.ini 中的日志配置（仅在实际存在配置文件时）。
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)
# 从应用配置读取数据库 URL，写入 Alembic 主配置，保证与应用连接同一数据库。
settings = FinanceClawSettings()
database_url = normalize_database_url(settings.database_url.get_secret_value())
ensure_database_parent(database_url)
config.set_main_option("sqlalchemy.url", database_url)
# 迁移比对的目标 metadata：与自动建表使用同一份。
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式执行迁移：不连接数据库，仅生成带字面量参数的 SQL 脚本。

    使用场景：DBA 审核或在受限网络环境中先行出 SQL，再人工执行。
    """
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
    """在线模式执行迁移：建立真实连接，在事务内应用全部待执行版本。

    使用场景：常规 ``alembic upgrade``；使用 NullPool 避免迁移进程
    残留连接池。
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


# 依据 Alembic 调用模式选择执行路径：--sql 为离线，其余为在线。
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
