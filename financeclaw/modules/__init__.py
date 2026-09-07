"""领域模块聚合层。

按业务能力聚合会话、执行、交互、委派、记忆、制品、审计、事件、流程与紫微。
当前采用模块内聚的实现方式：models/policy/service 与 SQLAlchemy repository/tables
可以同处一个领域包。跨模块协作及必要的原子写入见 docs/architecture/package-layout.md；
本层不得导入 HTTP、应用用例或 Agent 编排代码。
"""
