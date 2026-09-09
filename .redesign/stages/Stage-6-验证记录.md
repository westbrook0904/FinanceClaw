# Feishu 验证入口

保留渠道消息解析、配置和生命周期测试；当前执行及通知集成由 tests/stage8 与 tests/stage8_hotfix 验证。测试使用合成身份和网关，不发送真实飞书消息。

外部渠道的实际回执与去重窗口须单独验收，见 [通知手册](../../docs/operations/notifications.md)。
