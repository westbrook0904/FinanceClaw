"""FinanceClaw 对外接口层（interfaces）：承载面向外部调用方的协议适配入口。

http 负责 REST/SSE、认证和应用生命周期，channels 负责飞书 SDK 的事件与回复适配。
两种入口复用 application 的会话和交互用例；新增渠道不应复制业务执行或审批规则。
"""
