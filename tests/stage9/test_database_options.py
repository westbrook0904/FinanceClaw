"""确保数据库超时配置不会覆盖部署提供的 schema 隔离选项。"""

from sqlalchemy import create_engine

from financeclaw.shared.infrastructure import database as module


def test_timeout_preserves_dsn_options(monkeypatch):
    """构建连接参数即可验证该回归，不需要访问真实数据库。"""
    seen = {}

    def isolated_engine(url, **kwargs):
        """记录 PostgreSQL 连接参数，返回仅用于释放资源的 SQLite 引擎。"""
        seen.update(kwargs)
        return create_engine("sqlite://")

    monkeypatch.setattr(module, "create_engine", isolated_engine)
    database = module.ApplicationDatabase(
        "postgresql+psycopg://probe@localhost/probe?options=-csearch_path%3Disolated",
        statement_timeout_seconds=12,
    )
    try:
        assert (
            seen["connect_args"]["options"] == "-csearch_path=isolated -c statement_timeout=12000"
        )
    finally:
        database.close()
