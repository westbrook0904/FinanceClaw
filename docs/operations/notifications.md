# 飞书任务卡与通知交付

飞书连接和通知发送在 integrations 角色运行。API 只持久化消息/卡片决定与通知意图；Worker 只执行图。每个 Turn 固定一张任务卡和原始回复目标。

## 启动

使用统一 `compose.yml`。空应用库为 14 张表，原有开发库不会自动删除或升级。配置飞书 APP_ID、APP_SECRET、open_id allowlist 和 scopes；开启机器人、WebSocket 的 `im.message.receive_v1`/`card.action.trigger` 及 CardKit 权限。生产使用 strict 模式。

`python -m financeclaw.integrations` 同时监督渠道、通知和历史消费者，默认只有一个实例建立 WebSocket。API 扩容不会增加连接数量。integrations 健康检查分别报告渠道、通知和历史任务；心跳不代替消息发送回执。

## 事务与投递

| 表 | 责任 |
|---|---|
| notification_targets | 每个 Turn 固定原消息订阅与会话绑定，保存 CardKit ID、卡片消息 ID、确认序号和期望视图 |
| notification_events | 业务状态事务冻结卡片视图；最终正文从同事务 Journal 冻结 |
| notification_deliveries | 每次卡片操作或文本分片的固定内容、摘要、UUID、租约与渠道回执 |
| notification_senders | 发送者应用身份及心跳 |

卡片先创建实例，再 reply 原消息发布引用，后续以同一 CardKit ID 全量更新。未尝试的过时视图合并跳过；已经尝试且未知的视图保留原内容和操作键，后续卡片更新不得越过它。卡片回调的重复点击回执复用 `audit_records`；决定保存在 interactions，新恢复命令保存在 turn_commands。

只有统一 API 业务事务提交后才确认受理。停止请求先关闭后续派发和待答交互，卡片显示“正在停止”；远端确认结束后显示“已停止”。飞书原生发送按钮没有被替换，停止入口位于任务卡。撤权封闭后续权限，仍允许观察已经产生的结果。

最终文本按 UTF-8 字符边界每片 3500 字节，另加片号；每片固定独立 UUID。前片未明确成功，后片不发送。失败不重新提交 start/resume，也不重跑模型。终态和已回答卡片关闭旧按钮；展示存在延迟时，受理层仍会拒绝过期或冲突决定。

发送前检查本地绑定、允许列表、视图生命周期与内容摘要，并通过 GetMessage 核对原消息的 chat、sender、tenant 和撤回状态。消息发送后才撤回目标，不能收回已经发生的外部调用；回执仍必须保存。

| 状态 | 处理 |
|---|---|
| pending / retry | 尚未发送或明确可重试，原键和原内容退避，默认明确失败上限 5 次 |
| sending | 已提交发送责任；进程丢失后视为 uncertain |
| sent | 首发取得匹配的消息 ID、chat/parent；更新取得明确成功并保存原卡片消息 ID |
| uncertain | 丢响应、回执不完整或更新结果无法确认；不当作未发送 |
| dead_letter | 明确永久拒绝、失败耗尽或冻结内容损坏 |
| suppressed | 收件绑定失效、静音禁止首发，或未投递视图/交互已过时 |

`notification_verified_dedup_seconds=0` 时未知消息操作停止自动发送。非零恢复窗口必须有 `notification_dedup_evidence`；首次尝试冻结窗口与证据摘要，仅在窗口内原 UUID、原内容、原目标恢复。不能重新部署后延长窗口，不能换键重发，后来的明确拒绝也不能抹去先前可能送达的事实。

卡片创建尚未发布，失败可以重试创建，可能留下不可见孤立实例；实例 ID 绑定持久化后才允许回复消息。已确定未发布的旧实例不会让新视图引用错误内容。未知首发/更新则进入上述 uncertain 处理。

## 查询与操作

读取 `GET /v1/conversations/{id}/turns/{turn_id}` 查看当前快照，读取 Journal 核对最终正文。卡片和自然语言回答都调用同一 interaction 受理事务；重复事件复用原回执，不重新发送 resume。

取消只表示已收到停止意图，原生停止未确认前显示“正在停止”。不确定的发送不能静默当作未发送。已回答或过期卡片即使仍显示旧按钮，API 也会拒绝冲突决定。

## SSE 与验证

SSE 只有最新 `turn.snapshot` 和心跳；不存在进度事件历史表。客户端断开不影响通知、执行或最终 Journal。通知细节与原生运行信息不进入公开快照。

回归测试位于 `tests/stage8/test_notifications.py`、`tests/stage8/test_notification_sdk.py`、`tests/stage8_hotfix/test_feishu_cards.py` 和 Stage 10 测试目录。它们保留了发送不确定性、分片顺序、目标归属和卡片重放的验证；真实飞书发送仍需要独立的合成 canary，不属于自动本地测试。
