# 飞书任务卡与通知交付

飞书开启后，BFF 自动启动持久化通知发送循环：每轮一个可更新任务卡，完成正文回复原消息。卡片支持停止、授权/撤权、输入、单选/多选与审批。详细协议和代码入口见[实施记录](../../.redesign/stages/Feishu-交互卡片适配实施方案.md)。

## 启动

1. 使用新空业务库执行 `.venv/bin/alembic upgrade head`。当前唯一初始 schema 为 20 张表；本机旧开发库不会自动删除或升级。
2. 配置 `FINANCECLAW_FEISHU_ENABLED=true`、应用凭证、允许使用的用户列表与权限范围。BFF 就绪后直接受理，通知无单独开关。
3. 飞书后台启用机器人、同一 WebSocket 的 `im.message.receive_v1` 与 `card.action.trigger`、消息读取/回复和 CardKit 创建/更新权限，发布权限变更。
4. 启动 BFF。默认空闲通知轮询 0.2 秒，Channel、执行循环和通知循环统一随 lifespan 启停。

必要时可按 [notifications.env.example](../../config/environments/notifications.env.example) 和 [compose.notifications.yml](../../compose.notifications.yml) 启动额外发送进程：

```bash
.venv/bin/python -m financeclaw.bff.notifications.worker
```

独立发送器不建立 WebSocket，与 BFF 内置发送器共用应用身份、当前允许列表、数据库和租约。该独立角色的 `FEISHU_ENABLED=false` 只表示不启用入站连接。BFF `/ready` 检查 `bff_runs`、`feishu_channel` 和 `notification_sender`；心跳不代替消息回执。

## 事务与投递

| 表 | 责任 |
|---|---|
| notification_targets | 每根固定原消息订阅与会话绑定，保存 CardKit ID、卡片消息 ID、确认序号和期望视图 |
| notification_events | 业务状态事务冻结卡片视图；最终正文从同事务 Journal 冻结 |
| notification_deliveries | 每次卡片操作或文本分片的固定内容、摘要、UUID、租约与渠道回执 |
| notification_senders | 发送者应用身份及心跳 |

卡片先创建实例，再 reply 原消息发布引用，后续以同一 CardKit ID 全量更新。未尝试的过时视图合并跳过；已经尝试且未知的视图保留原内容和操作键，后续卡片更新不得越过它。卡片回调复用 `run_inbox` 保存事件及重复点击回执；没有新增回调表。

只有 BFF 业务事务提交后才确认受理。停止请求先关闭后续派发和待答交互，卡片显示“正在停止”；远端确认结束后显示“已停止”。飞书原生发送按钮没有被替换，停止入口位于任务卡。撤权封闭后续权限，仍允许观察已经产生的结果。

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

- `GET /v1/runs/{root_id}/notifications`：原主体读取订阅、已确认卡片序号、待消费数及各投递状态。
- `DELETE /v1/runs/{root_id}/notifications` 或 `/mute <root_id>`：停止后续新通知；已有任务卡允许更新收尾，Agent 继续执行。
- `/cancel <root_id>`、`/authorize <root_id>`、`/revoke <root_id>`：与卡片按钮共用业务控制入口。
- `/answer`、`/choose`、`/approve`、`/reject`：显式命令共用交互受理事务；单一 text 澄清可直接回复。
- `NotificationRepository.metrics()`：按状态统计数量与最早更新时间；关注积压、uncertain 年龄、dead_letter 与缺失心跳。

卡片回调检查应用、原租户/用户/会话/消息、可见按钮、交互版本、Schema、有效授权及动作摘要。普通“同意”不会批准。重复点击与重推不重复恢复，不给下一题提交旧答案；改变同一决定键的输入会被拒绝。输入不合规会提示检查输入并使用原单聊最新卡片。

地址、卡片和表单正文沿用 Journal 的访问控制。不要手动重置发送键或把 sending/uncertain 改回 pending；缺乏渠道侧确认时保留原记录，最终结果仍可从 Journal 查询。当前不自动回收通知和命令回执证据。停止服务会保留未完成的持久责任。

## SSE 恢复

`GET /v1/runs/{root_id}/events` 的事件 ID 为 `<root_id>:<revision>`，重连时通过 `Last-Event-ID` 提交。
每个客户端独立读取，不消费其他订阅者的事件。遗漏进度按 revision 回放，当前事件携带
`snapshot=true`；终态继续使用 `assistant.completed` 等既有事件类型，正文来自 Journal。
历史进度仅含安全摘要，不重放旧审批动作或 token。

非法、跨根、未来游标和历史缺口分别通过快照中的 `reset_reason` 表示。
单次超过 256 条历史进度也回到当前快照。运维可按保留策略回收旧 `run_progress_events`，
当前投影与 Journal 必须保留；本阶段没有自动回收通知证据。
SSE 断开、重连及任意数量观察者不会写执行事实或增加 backend 请求。

## 验证范围

固定 `lark-channel-sdk==1.4.0`，离线测试运行真实 SDK 请求模型、HTTP 序列化与回调响应对象，仅替换外部网络；同时覆盖 BFF 事务、原任务恢复、停止与重复点击。真实租户的客户端表单表现、回调耗时、服务限制及幂等窗口仍需单聊联调，不能把 MockTransport 测试当作真实送达。

官方参考：[回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply)、[卡片回调通信](https://open.feishu.cn/document/feishu-cards/card-callback-communication)。
