"""领域模块聚合层。

聚合 FinanceClaw 的各业务领域模块（artifacts、audit、conversation 等），向应用层
暴露统一的模块边界；本层只允许依赖 kernel、模块内部代码与共享 ORM 基类。
"""
