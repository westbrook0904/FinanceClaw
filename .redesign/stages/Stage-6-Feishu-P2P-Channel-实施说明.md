# Feishu P2P Channel

飞书入站属于 BFF：按 app、tenant、open_id、chat_id 绑定 Conversation，message_id 为 Turn 幂等键，白名单内 P2P 文本进入顶层 Agent。一个 chat 串行处理，不同 chat 有界并发。

人工交互使用 /answer、/choose、/approve、/reject；/cancel 请求取消，/mute 关闭后续通知。所有决定复用 BFF 交互契约与有限授权。

开启持久通知时，前台受理完成即可退出，独立发送器交付结果与问题。未开启时使用当前 BFF SSE 进度与 Journal 最终文本。BFF 后台执行不依赖展示连接。

部署与回执边界见 [通知手册](../../docs/operations/notifications.md)。
