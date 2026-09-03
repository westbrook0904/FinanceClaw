"""FinanceClaw 平台的包顶层入口，仅对外暴露组合根提供的装配 API。

FinanceClaw 是构建在 LangChain 之上的金融领域 Agent 平台；应用启动与测试
通过本包获取 ``build_components`` 装配出的组件集合。
"""

from financeclaw.bootstrap import FinanceClawComponents, build_components

# 包对外导出的符号清单。
__all__ = ["FinanceClawComponents", "build_components"]
