# M5c：排空阻塞项定位

日期：2026-10-15。基线为 M5b 本地提交 `07a9bfb`，未推送。在 [M5b 排空计数](m5b-admission.md) 上增加只读分页明细，帮助从“还有积压”定位到具体 Turn、通知、绑定或记忆作业。执行、审批、取消仍走现有 AgentScope 和业务接口，不增加恢复调度器或自动确认机制。

## 查询入口

`GET /v1/operations/drain/items?kind=turns&limit=50`，沿用 Bearer 身份和当前 `(tenant, subject)` 范围。响应为：

```json
{"scope":"current_owner","items":[],"next_after":null}
```

`kind` 可取以下四种值；默认 `turns`。`limit` 为 1–100，默认 50。存在下一页时，将 `next_after` 原样作为 `after` 参数；没有下一页时为 null。

| kind | 明细内容 | 对应排空计数 |
| --- | --- | --- |
| `turns` | Turn ID、原会话 ID、状态、revision、原因码、Command 状态及未决交互/副作用/Outbox 数量 | queued、running、waiting、cancelling、blocked、active_commands、pending_interactions、uncertain_effects、pending_outbox |
| `notifications` | 通知 ID、原会话与 Turn ID、状态、revision | pending_notifications、unverified_notifications |
| `notification_bindings` | 绑定内部 ID、当前 Turn 与原会话 ID、Turn 状态和 revision | unprojected_notifications |
| `memory_jobs` | 作业 ID、类型、状态、epoch、revision、尝试次数和固定错误码 | pending_memory_jobs |

`pending_forget` 是当前主体的单条隐私状态，继续用既有 `GET /v1/memory/privacy` 定位，不新增相同接口。查询不需要先暂停，也不会改变受理状态。

任务明细每个 Turn 一行；一个 Turn 可以对应多个计数，不应将明细行数当作阻塞项总和。即使 Turn 已 completed/failed/cancelled，只要命令未完成或还有 pending 交互、副作用、Outbox，就仍出现在明细中。

通知的会话 ID 来自原 Turn，不会因飞书绑定切换到新会话而改变。通知/绑定查询同时校验绑定和 Turn 的 owner；排空通知计数使用同样的归属范围。未投影绑定的筛选条件与现有渠道投影队列一致，并在明细与计数中共用。

明细不返回用户消息、模型结果、工具参数/结果、交互问题、通知正文、飞书用户/chat 标识、记忆来源或 lease token。完整业务内容仍由原有受控接口访问。

## 分页与一致性

分页按不可变 ID 前进。任务、通知和绑定按内部文本 ID 排序；记忆作业 ID 虽在 JSON 中表示为字符串，查询仍按数据库 bigint 排序，避免 `100` 排在 `9` 前而漏项。每页多读取一行以判断是否有后续页，最多返回 100 条。

前一页的记录已被处理或消失时，后续页不依赖 offset，不会因此错位跳过原有后续记录。游标仅为位置，不携带授权；修改游标不能绕过当前主体过滤。无效类型、负数/溢出的记忆 ID、过长位置或未知 kind 返回 422。

各页和排空计数是独立查询时点的观察，不是一份冻结快照。并发出现的新阻塞项可能排在已读游标之前，需要从首页重新扫描。最终重新读取排空状态，必要时停止相关写入后核对；空页或 `next_after=null` 本身不能证明可以切换。

## 示例与后续处理

```bash
BASE_URL=http://localhost:8080
curl --fail --silent --get "$BASE_URL/v1/operations/drain/items" \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" \
  --data-urlencode 'kind=turns' --data-urlencode 'limit=20' | python3 -m json.tool

# AFTER 使用上一页返回的 next_after；为 null 时停止翻页。
curl --fail --silent --get "$BASE_URL/v1/operations/drain/items" \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" \
  --data-urlencode 'kind=turns' --data-urlencode "after=$AFTER" \
  --data-urlencode 'limit=20' | python3 -m json.tool
```

得到 `conversation_id` 和 Turn `id` 后，可继续查询 `GET /v1/conversations/{conversation}/turns/{turn}` 和该 Turn 的 `/interactions`，按原协议提交答案或取消。此查询不会自动批准、取消、重投或补写成功。

`uncertain_effects` 和 `unconfirmed_execution` 仍需原有的人工核对边界，不能用新 key 重跑来消除积压。飞书 `unknown` / `submitted_unverified` 保留原生 SDK 的回执限制。记忆作业 `extraction_failed` / `index_failed` 应结合模型配置与已有 Worker 处理；用户遗忘请求才触发隐私清理。

## 验证

```bash
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/test-live-acceptance.py
python3 scripts/collect-evidence.py
python3 scripts/check-delivery.py
```

新增 [BacklogIT](../../src/test/java/com/financeclaw/m5/BacklogIT.java) 使用真实 PostgreSQL，覆盖认证和分页参数、不同租户/用户隔离、前页消失后的连续读取、六类非终态、四类终态残留事实、通知会话切换与内容最小化、投影 revision/静音/抑制、跨 owner 错配、记忆数字 ID 排序以及查询前后全表指纹不变。

本阶段完整 Java 回归 **659 项通过**（495 单元/契约、164 集成），零失败、零错误、零跳过，格式检查通过；包含新增 17 项 Backlog 集成测试。另有 **8 项离线验收器契约测试通过**。机器证据和源码指纹见 [M5c 本地记录](evidence/m5c-local-results.json)。

没有新增数据库迁移或依赖。M5b 的 14 项 Compose 记录属于其历史 JAR；本阶段不将历史结果算作当前 JAR 的容器复测。完整 Java 回归包含原有独立 JVM 和实际镜像五工具测试。真实环境、线上切换与紫微普通启用仍保留原门禁。

代码入口：[OperationsController](../../src/main/java/com/financeclaw/api/OperationsController.java)、[OperationsService](../../src/main/java/com/financeclaw/application/OperationsService.java)、[OperationsMapper](../../src/main/java/com/financeclaw/infrastructure/task/OperationsMapper.java)。
