# Stage 6：飞书 P2P Channel 实施说明

状态：Proposed
编制日期：2026-09-04

## 1. 目标

为 FinanceClaw 增加一个轻量的飞书 Channel，使用户在飞书机器人单聊中可以：

1. 发送文本消息并创建 FinanceClaw Conversation Turn；
2. 在同一飞书单聊中复用 Conversation，保留多轮上下文；
3. 通过飞书流式消息卡片查看 Agent 回复；
4. 依靠飞书消息 ID 和 BFF 幂等机制避免重复执行。

本阶段只交付可联调、可灰度、可继续演进的单聊闭环，不建设通用消息平台、第二套运行时或独立微服务。

结论：可以实施，整体复杂度为中等。一期的最小改动面是一个进程内飞书适配器、一个会话绑定表和一次 run 级流式订阅修复。

## 2. 一期边界

### 2.1 包含

- 飞书企业自建应用；
- WebSocket 长连接接收 im.message.receive_v1；
- chat_type=p2p 的用户文本消息；
- 一个飞书单聊稳定映射到一个 FinanceClaw Conversation；
- 飞书流式 Markdown 卡片回复；
- 同一单聊串行处理，不同用户可以并行；
- 重复事件幂等、最终结果校正和可见失败降级。

### 2.2 不包含

- 群聊、话题群和 @机器人策略；
- 图片、文件、音视频等非文本输入；
- 飞书卡片审批和 HITL 恢复；
- 子 Agent 或 Workflow 的 token 级转发；
- Channel 独立部署、Webhook 模式和多实例主选；
- 持久化入站队列、业务事件回放日志和多 Server Run 聚合表；
- 用户主动新建或切换多个飞书会话。

如果运行进入人工审批中断，一期只提示用户到 Web/API 完成审批，不在飞书内继续恢复。

## 3. 复杂度审视

### 3.1 必须完成

| 项目 | 复杂度 | 必要性 |
|---|---:|---|
| 修复 Agent Server 真实流式订阅 | 中 | 现有 threads.stream 调用与 SDK 0.4.4 不兼容 |
| 飞书单聊与 Conversation 映射 | 低 | 多轮上下文所需的最小持久化状态 |
| 飞书 SDK 长连接与生命周期 | 中 | 收发消息的必要适配 |
| Turn 幂等和同聊天串行 | 低 | 防止事件重推和共享 thread 并发串扰 |
| 流式卡片与最终结果兜底 | 中 | 保证体验和最终内容完整 |

### 3.2 明确延后

以下设计会明显增加数据表、状态机和部署复杂度，一期不做：

- 持久化 Channel Inbox 和租约消费者；
- 一个业务 run 对多个 Server Run 的 token 级聚合；
- SSE Last-Event-ID 的跨进程业务回放；
- 自研 CardKit sequence 控制器和卡片状态表；
- 通用 MessagingChannel SPI 和 Channel Registry；
- 独立 Channel 服务、代理令牌和分布式主选。

一期依靠飞书 SDK 基础去重、BFF Turn 幂等以及最终状态查询，保证不重复执行和最终回复完整。进程在飞书事件已确认、Turn 尚未受理的极小窗口内退出时，消息可能丢失；灰度期接受该限制。若要承诺生产零丢失，再引入持久化 Inbox。

## 4. 架构决定

### 4.1 部署形态

一期把 Feishu Channel 内嵌在 BFF 进程中，作为与 HTTP 并列的入站协议适配器：

    Feishu WebSocket
      -> interfaces/channels/feishu.py
      -> application/feishu_channel_service.py
      -> ConversationService
      -> Agent Server
      -> application StreamEvent
      -> Feishu streaming card

Channel 直接消费与 HTTP SSE 同源的 StreamEvent，不请求本机 HTTP 端点，不重复执行 Bearer 认证和 SSE 文本解析。

一期只允许一个 BFF 实例开启 FEISHU_ENABLED。需要独立扩缩容时再拆分 Channel worker。

### 4.2 依赖选择

使用飞书官方 lark-channel-sdk 的 FeishuChannel，复用其：

- WebSocket 连接和自动重连；
- 入站消息规范化和基础去重；
- 单聊及发送策略；
- channel.stream 流式 Markdown 卡片；
- 出站节流、重试和错误归一化。

不自行实现飞书 token 缓存、WebSocket 协议、CardKit 序列号或通用重试器。

参考资料：

- [飞书 Echo Bot 教程](https://open.feishu.cn/document/develop-an-echo-bot/introduction)
- [lark-channel-sdk Quickstart](https://github.com/larksuite/channel-sdk-python/blob/main/docs/quickstart.md)
- [lark-channel-sdk Channel Reference](https://github.com/larksuite/channel-sdk-python/blob/main/docs/reference.md)
- [CardKit Streaming](https://github.com/larksuite/channel-sdk-python/blob/main/docs/cardkit-streaming.md)

## 5. 最小数据模型

仅新增 channel_conversation_bindings 表：

| 字段 | 说明 |
|---|---|
| binding_id | 内部主键 |
| channel | 固定为 feishu |
| app_id | 飞书应用 ID，不是 Secret |
| tenant_key | 飞书租户标识 |
| external_user_id | 发件人 open_id |
| external_chat_id | P2P chat_id |
| conversation_id | FinanceClaw Conversation 外键 |
| created_at / updated_at | 审计和运维时间 |

唯一约束为 channel、app_id、tenant_key、external_chat_id 的组合。

验证后的飞书事件映射为可信执行身份：

    tenant_id  = "feishu:" + tenant_key
    subject_id = "feishu:" + sender.open_id

tenant_key 和 open_id 不从正文、Prompt 或普通 HTTP Header 推导。

## 6. 流式主干前置修复

### 6.1 当前问题

现有 LangGraphAgentServerClient.stream_thread 把 SDK 0.4.4 的 AsyncThreadStream 当成 AsyncIterator，真实运行时会在 async for 处抛出 TypeError。

此外，一个 Conversation 的多个 Turn 共享 thread，按 thread 订阅与 /v1/runs/{run_id}/events 的 run 级语义不一致。

### 6.2 一期修复

1. 将 Agent Server Port 改为 stream_run(thread_id, server_run_id)；
2. 真实客户端使用 runs.join_stream；
3. Run、Workflow 和 Conversation Service 使用已绑定的 server_run_id；
4. 至少请求 messages、updates、values 流模式；
5. 对外只暴露少量稳定事件：

       assistant.delta
       run.progress
       assistant.completed
       run.interrupted
       run.failed

6. 流结束后通过现有状态/结果路径校正最终内容，并确保 Conversation Journal 只落库一次。

`runs.join_stream` 不回放订阅建立前已经产生的 token，因此一期不承诺每个中间 token 都可见；卡片结束前必须查询最终状态并用完整结果校正内容。这样能保证最终答案正确，同时避免提前建设事件回放日志。

一期不做跨 Server Run token 聚合。发生 delegation 时，Channel Service 复用 ConversationService.status 推进子运行，卡片显示简短进度，完成后一次性写入最终答案。

## 7. 消息处理流程

### 7.1 接收

1. SDK 接收并验证事件；
2. 丢弃机器人和系统自发消息，避免循环回复；
3. 非 P2P 消息直接忽略；
4. 非文本消息回复“当前仅支持文本”，不进入 Agent；
5. 把后续执行放入受并发上限约束的进程内任务，避免事件回调长期占用。

### 7.2 受理

1. 根据飞书单聊键查找 Conversation 绑定；
2. 不存在时创建 Conversation 并原子建立绑定；
3. 使用 feishu:{app_id}:{message_id} 作为 Turn Idempotency-Key；
4. 将规范化纯文本作为 ConversationTurnRequest.message；
5. 使用按 chat_id 分组的内存锁保证同一单聊同时只运行一个 Turn；不同单聊共享总并发上限。

### 7.3 回复

1. 使用 channel.stream 创建回复原消息的 Markdown 流式卡片；
2. assistant.delta 追加文本；
3. run.progress 只展示简短状态，不暴露 Tool、Prompt 或内部推理；
4. assistant.completed 使用最终完整回复校正已显示内容；
5. run.failed 或卡片更新失败时，尝试发送普通文本错误消息；
6. 最终内容以 Conversation Journal 或运行最终输出为准。

## 8. 代码落点

预计新增：

    financeclaw/interfaces/channels/__init__.py
    financeclaw/interfaces/channels/feishu.py
    financeclaw/application/feishu_channel_service.py
    financeclaw/infrastructure/migrations/versions/0006_feishu_channel.py
    tests/stage6/__init__.py
    tests/stage6/test_feishu_channel.py
    tests/stage6/test_run_streaming.py

预计修改：

    financeclaw/application/ports/agent_server.py
    financeclaw/infrastructure/clients/agent_server.py
    financeclaw/application/run_service.py
    financeclaw/application/workflow_service.py
    financeclaw/application/conversation_service.py
    financeclaw/modules/conversation/tables.py
    financeclaw/modules/conversation/repository.py
    financeclaw/infrastructure/settings.py
    financeclaw/interfaces/http/app.py
    financeclaw/bootstrap.py
    pyproject.toml
    uv.lock
    config/environments/*.env.example

不新增通用 Channel Registry、通用事件总线或新的 Runtime 包。

## 9. 配置与安全

最小配置：

    FINANCECLAW_FEISHU_ENABLED=false
    FINANCECLAW_FEISHU_APP_ID=
    FINANCECLAW_FEISHU_APP_SECRET=
    FINANCECLAW_FEISHU_ALLOWED_OPEN_IDS=[]
    FINANCECLAW_FEISHU_SCOPES=["market:read","tools:read","artifacts:read","memory:read"]
    FINANCECLAW_FEISHU_MAX_CONCURRENCY=8

要求：

- APP_SECRET 使用 SecretStr，生产由 Secret Manager 或 Kubernetes Secret 注入；
- 灰度期必须配置 open_id 白名单；
- 飞书用户的 FinanceClaw scopes 显式配置且默认只读；
- 不记录 App Secret、完整事件原文或未脱敏消息正文；
- 联调先启用 SDK audit security mode，处理告警后生产切换 strict mode；
- SDK 仅在 Channel 开启时延迟加载，未安装或配置不完整应启动失败并给出明确错误；
- Channel 默认关闭，关闭时不影响现有 HTTP BFF。

飞书后台需开启机器人、WebSocket 事件订阅、接收消息、机器人发消息和 CardKit 创建/更新权限；权限变更后重新发布并安装应用。

## 10. 异常与降级

| 场景 | 一期处理 |
|---|---|
| 重复飞书事件 | SDK 去重加 Turn 幂等，不新建 Turn |
| 用户连续发消息 | 同 chat_id 内存锁串行处理 |
| Agent Server 流中断 | 查询最终状态；已完成则输出最终文本，否则显示失败 |
| 飞书卡片更新失败 | 降级发送普通文本最终结果 |
| 进入普通 HITL | 提示到 Web/API 审批并结束当前卡片流 |
| 进入 delegation | 显示进度并轮询业务状态，最终结果一次性更新 |
| BFF 进程重启 | 已落库 Turn 由现有对账恢复；未落库入站消息一期不保证 |

## 11. 测试计划

### 11.1 自动化测试

- stream_run 使用 server_run_id 并返回真正的异步迭代器；
- 飞书事件只接受 P2P 用户文本；
- 飞书租户和用户身份映射正确；
- 并发创建同一绑定时只产生一个 Conversation；
- 重复 message_id 返回原 Turn；
- delta、完成、失败和中断事件映射正确；
- 飞书更新失败时触发文本降级；
- 两个飞书用户的 Conversation 和流完全隔离；
- 同一用户快速发送两条消息时顺序稳定；
- 最终飞书文本与 Conversation Journal 一致；
- Alembic upgrade、downgrade、re-upgrade 通过。

### 11.2 真实联调

- 使用飞书测试企业和单一 open_id 白名单；
- 验证长连接重连、消息重推、卡片流式更新和文本降级；
- 没有真实飞书凭证时外部测试显式 skip，不使用 mock 冒充在线证据。

## 12. 验收标准

- 白名单用户发送私聊文本后能看到一条流式回复卡片；
- 后续消息复用原 Conversation 并使用前文；
- 重复投递不重复调用 Agent 或追加消息；
- 不同用户之间不串 Conversation、run 或回复；
- 中间流失败时仍能输出最终结果或明确失败；
- Channel 默认关闭且不改变现有 BFF 行为；
- Ruff、全量 pytest 和 Alembic 往返迁移全部通过；
- 完成一次真实飞书 P2P 端到端验证。

## 13. 实施顺序与预估

建议分成三个可独立验证的切片：

1. Run 级流修复：修正 Port/SDK 调用、稳定事件和最终落库；
2. 飞书单聊入站：长连接、文本过滤、Conversation 绑定和 Turn 幂等；
3. 流式回复与联调：卡片流、串行控制、降级、观测和真实环境验证。

代码与自动化测试属于中等改动，预计约 3 至 5 个工程日。飞书权限申请、应用发布和真实环境联调受外部流程影响，不计入代码完成时间。

## 14. 后续升级触发条件

- 要求事件确认后的零丢失：增加持久化 Inbox；
- 要求审批恢复或 delegation 全程 token 流：增加业务 run 片段聚合；
- Channel 需要独立扩缩容：拆分 worker 并引入可信服务身份；
- 需要多个 IM 平台：抽取最小 MessagingChannel Port；
- 需要群聊、媒体或审批卡片：分别建立新的垂直切片，不扩张本阶段范围。
