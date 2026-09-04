# Stage 6 验证记录

验证日期：2026-09-04

## 1. 已完成范围

- BFF 进程内接入官方 `lark-channel-sdk 1.4.0`，以 WebSocket 接收飞书事件，默认关闭并在启用时延迟加载；
- 只受理白名单用户的 P2P 文本消息，过滤群聊、机器人、系统消息，非文本输入返回可见提示；
- 从 SDK 已验证事件提取 `tenant_key`，与 `open_id`、`chat_id` 一起映射为可信 FinanceClaw 身份；
- 新增 `channel_conversation_bindings`，原子建立飞书单聊到 Conversation 的稳定绑定；
- 使用 `feishu:{app_id}:{message_id}` 复用持久化 Turn 幂等，同一 chat 串行、不同 chat 有界并行；
- Agent Server 改为 `runs.join_stream(thread_id, server_run_id)`，请求 `messages/updates/values` 并投影为五类稳定事件；
- 使用 Markdown 流式卡片回复，最终以 Conversation Journal 完整答案覆盖校正，卡片失败时降级普通文本；
- lifespan 管理连接、排空与断开，`/ready` 在启用时纳入飞书 Channel 健康状态；
- 配置按 development/test/staging/production 分离，生产启用时强制 `strict` security mode。

## 2. 自动化证据

```text
ruff check financeclaw tests scripts
All checks passed!

ruff format --check financeclaw tests scripts
158 files already formatted

pytest -q tests/stage6
9 passed, 1 skipped

pytest -q
90 passed, 2 skipped

uv lock --check
Resolved 148 packages

alembic heads
0006_stage6 (head)

git diff --check
通过

uv build --out-dir <temporary-directory>
Successfully built financeclaw-0.1.0.tar.gz and financeclaw-0.1.0-py3-none-any.whl
```

`tests/stage6` 覆盖真实 SDK 嵌套消息结构、可信 tenant 提取、P2P 准入、机器人过滤、
Conversation 复用、重复消息幂等、同 chat 串行、跨 chat 隔离、最终 Journal 校正、CardKit
文本降级、配置 fail-closed，以及 `0006_stage6 -> 0005_stage5 -> 0006_stage6` 迁移往返。

全量测试中的两个 skip 分别是 Stage 3 的可选 PostgreSQL 场景，以及未配置
`FINANCECLAW_FEISHU_E2E_*` 凭证时的真实飞书 WebSocket 探针。SDK 自带 protobuf/WebSocket
模块在 Python 3.13 下报告两条弃用告警，不影响本阶段门禁，但后续升级 SDK 时应复查。

## 3. 已验证的 SDK 契约

- `FeishuChannel.connect_until_ready` / `disconnect` 可用于应用生命周期；
- `InboundMessage` 的身份与聊天字段位于 `sender` 和 `conversation`，适配器已按真实类型测试；
- `channel.stream(chat_id, {"markdown": producer}, {"reply_to": message_id})` 支持 Markdown 流；
- Markdown 控制器同时提供 `append` 与 `set_content`，可在结束时进行完整内容校正；
- `channel.send` 支持 `reply_to` 与稳定 `uuid`，用于普通文本幂等降级；
- LangGraph SDK 的 `runs.join_stream` 返回 `AsyncIterator`，并接受 run ID 与多个 stream mode。

## 4. 尚需真实环境关闭的验收项

仓库中没有飞书测试企业凭证，也未改动飞书开放平台配置，因此不能把 mock/本地自动化结果
视为真实 P2P 端到端通过。正式灰度前仍需：

1. 在测试企业创建并发布企业自建应用，启用机器人和 `im.message.receive_v1` WebSocket 订阅；
2. 授予接收消息、机器人发消息及 CardKit 创建/更新权限，重新安装已发布版本；
3. 只配置一个 canary `open_id`，仅在一个 BFF 实例设置 `FINANCECLAW_FEISHU_ENABLED=true`；
4. 验证首次消息、多轮上下文、消息重推幂等、快速连续消息、断线重连和非文本提示；
5. 验证流式卡片最终内容与 Journal 一致，并实际演练 CardKit 失败后的普通文本降级；
6. 处理 SDK `audit` 告警后，将生产配置切换为 `strict`，再批准生产启用。

完成上述在线检查后，Stage 6 实施说明中的“真实飞书 P2P 端到端验证”验收项才可关闭。

只验证凭证与 WebSocket ready 状态时，可注入专用测试变量运行仓库探针：

```bash
FINANCECLAW_FEISHU_E2E_APP_ID=cli_xxx \
FINANCECLAW_FEISHU_E2E_APP_SECRET='<secret>' \
FINANCECLAW_FEISHU_E2E_OPEN_ID=ou_canary_user \
.venv/bin/pytest -q tests/stage6/test_feishu_live.py -m external
```

该探针不会创建 Conversation，也不代替由 canary 用户实际发消息完成的 P2P 验收。
