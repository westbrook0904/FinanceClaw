"""Alembic 迁移包：管理 FinanceClaw 数据库 schema 的版本化演进。

本包属于 infrastructure 层：``env.py`` 是 Alembic 运行环境，
``versions/`` 下每个文件对应一次 schema 演进；生产环境的 schema
变更只能经由本包执行，禁止自动建表。
"""
