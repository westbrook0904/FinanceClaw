# Stage 8B：飞书通知交付

通知发送器是 BFF 包中的独立运行角色，与 BFF、Coordinator 共用 `financeclaw_app`。
Coordinator 只通过 `shared/notifications` 写事务事实，不导入 BFF 或飞书 SDK。
不需要新增数据库、消息队列或 Temporal。

## 启用和部署

1. 对共享应用库执行 `.venv/bin/alembic upgrade head`，当前头为 `0010_stage8b`。
   本版本 Coordinator 启动必须具备通知表，即使新通知受理开关关闭，也不能漏掉已有责任。
2. 先升级 Coordinator，再升级 BFF。8B 新根固定 `driver_version=2`，8A Worker 只领取版本 1，
   不能处理这些新根；8B Worker 兼容原版本 1 的 8A 根，不将它们改成新的驱动身份。
   新 BFF 的就绪检查只承认版本 2 Worker 心跳。旧 legacy 根接管仍属于 8C。
3. 按 [`notifications.env.example`](../../config/environments/notifications.env.example) 设置独立发送角色。
   该片段中的 `FEISHU_ENABLED=false` 只适用于发送器，飞书入站 BFF 仍需 `FEISHU_ENABLED=true`。
   BFF 与发送器使用同一 app ID、当前灰度名单和数据库；凭据由环境注入。
4. 通过测试单聊验收后，在飞书 BFF 和发送器上启用 `FINANCECLAW_FEISHU_NOTIFICATIONS_ENABLED=true`。
   Coordinator 的通知写入取决于根的持久订阅，不需要飞书 app secret。

   ```bash
   .venv/bin/python -m financeclaw.bff.notifications.worker
   ```

   或使用 [`compose.notifications.yml`](../../compose.notifications.yml)。发送器不启动 WebSocket，
   可运行多实例，每实例一次只领取一个分片，长调用期间续租。SIGTERM 排空当前有限调用。
   BFF `/ready` 单独报告 `notification_sender`；这个检查表示 schema 和进程心跳可用，
   飞书 API 是否可投递由实际回执及积压状态判断。

通知开关默认关闭。关闭新受理不删除已有订阅，也不让已订阅任务切回前台卡片最终交付。
关闭发送器会保留待办；重新启动后继续原分片。

## 事务和交付语义

| 事实 | 写入边界 |
|---|---|
| `notification_targets` | 飞书首次受理与 Turn、用户 Journal、授权和固定命令同事务；每根只有原消息订阅 |
| `notification_events` | 根终态／交互／需处理停顿与进度同事务；完成正文从同事务 Journal 冻结 |
| `notification_deliveries` | BFF 通知订阅器将一个事件的所有分片和消费标记一起提交，冻结版本、正文、摘要、UUID |
| `notification_senders` | 独立发送者的版本和应用心跳 |

事件逐行消费，没有共用全局序号游标，因此迟提交的事务不会被较大序号跳过。
审计 Outbox 的 published 不等于飞书 sent。发送故障不重新提交 start/resume，也不重跑模型。
普通不变探测、child 的中间文字不会形成最终通知。

协调通知模式当前固定为 `text_reply_v1`。前台短受理后即返回，不创建最终卡片；回答、选择和
审批消息不会创建第二个最终目标。其他模式的 legacy 展示保持原行为。
正文按 UTF-8 字符边界每片最多 3500 字节，另加 `[当前/总数]` 前缀；原正文完整保留。
每片使用独立、永久固定的 UUID，只调用 SDK 底层 `areply`，不会触发高层分片或 reply→create 降级。
前片未明确 sent 时不会越过它发送后片；前片死信或被抑制时，后片也会被抑制。

发送前检查当前白名单、完整会话绑定和本地订阅是否仍有效，并用飞书 GetMessage 核对原消息
的 chat、sender open_id、tenant 和撤回状态。交互还检查当前 revision、pending、期限、取消和授权停顿。
已经进入网络的发送不能被后来的撤销收回，回执仍保存；用户决定入口会再次校验交互有效性。

| 状态 | 处理 |
|---|---|
| pending / retry | 尚未发送或明确可重试拒绝；使用原键、原正文和原方法退避，明确失败最多默认 5 次 |
| sending | 已提交发送责任；发送者硬退出或失去租约后按 uncertain 处理 |
| sent | SDK 返回成功、消息 ID 和匹配的 chat／parent；保存回执 ID |
| uncertain | 响应丢失、成功缺完整回执或结果无法分类；不能当作明确失败 |
| dead_letter | 明确永久拒绝、可重试失败耗尽，或冻结内容校验失败 |
| suppressed | 目标撤销、绑定变化、过期／已回答的交互或不可交付的后续分片 |

`notification_verified_dedup_seconds=0` 表示真实幂等窗口尚未验证，此时 uncertain 保留并停止自动发送。
配置非零窗口必须给出 `notification_dedup_evidence`；首发时保存窗口截止时间和证据摘要，
后续部署不能延长该分片的恢复窗口。在窗口内仅使用原 UUID／内容／目标恢复，并为超时保留余量。
窗口外保持 uncertain。后一次明确拒绝不能抹掉前一次可能已送达的事实。
SDK 的布尔 success 或“相同文本出现在聊天里”不能单独解决未知结果；本阶段不提供换键重发按钮。

参考官方[回复消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/reply)。
仓库已验证 `lark-channel-sdk 1.4.0` 的底层请求和回执解码；真实服务窗口另行验收。

## 查询、撤销与运维

- `GET /v1/runs/{root_id}/notifications`：原租户／主体读取订阅模式、未消费事件数、每片状态、次数和错误类别。
- `DELETE /v1/runs/{root_id}/notifications`：撤销原订阅；不取消 Agent，不改变接收人，不自动补发历史结果。
- 飞书 `/mute <root_id>`：同一单聊、原主体关闭后续通知。
- 普通回答与权限命令的简短受理反馈仍为入站应用的即时反馈；持久责任覆盖任务结果和需处理状态。
- 独立发送器 `NotificationRepository.metrics()` 提供每种状态的数量和最早更新时间；
  告警重点是未消费事件、pending/retry 积压、uncertain 年龄、dead_letter 和缺失心跳。

表中保存受保护的地址与通知正文，应沿用 Journal 的访问控制；日志只记录错误类型。
不要把发送键重置或将 sending/uncertain 手动改回 pending。出现 unknown 时，保留原记录，
依据原 SDK 回执或渠道侧可证明的投递记录核对。缺少证据时保持可见待处理，结果仍可从 Journal 获取。
有通知事实时迁移拒绝破坏性 downgrade；回滚应用须保留理解版本 2 根与通知表的协调／发送角色。

## SSE 恢复

`GET /v1/runs/{root_id}/events` 的事件 ID 为 `<root_id>:<revision>`，重连时通过 `Last-Event-ID` 提交。
每个客户端独立读取，不消费其他订阅者的事件。遗漏进度按 revision 回放，当前事件携带
`snapshot=true`；终态继续使用 `assistant.completed` 等既有事件类型，正文来自 Journal。
历史进度仅含安全摘要，不重放旧审批动作或 token。

非法、跨根、未来游标和历史缺口分别通过快照中的 `reset_reason` 表示。
单次超过 256 条历史进度也回到当前快照。运维可按保留策略回收旧 `run_progress_events`，
当前投影与 Journal 必须保留；本阶段没有自动回收通知证据。
SSE 断开、重连及任意数量观察者不会写执行事实或增加 backend 请求。

## 验收界限

本地测试覆盖正式数据库仓储、真实 PostgreSQL 多进程和官方 SDK 的 HTTP 协议边界。
真实飞书验收仍需明确授权的测试单聊和原消息：验证正常回执、同 UUID 去重、原消息撤回、
灰度名单撤销、长文本顺序、响应丢失后的原键恢复和幂等窗口边界。
未经这组验收，不声明 8B 已通过真实渠道发布门禁。
飞书 SDK 接收事件至业务受理事务提交前仍有进程内窗口；本阶段不会把执行 Inbox 当作渠道入站持久化。
