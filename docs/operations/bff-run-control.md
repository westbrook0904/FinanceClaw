# BFF 运行控制

BFF 管理顶层 start、人工 resume、cancel、有限授权与 Journal 收尾。Agent Server 运行顶层 ReAct，领域 Agent 与 Workflow 通过 Tool 在根图内调用。两边共用业务库和相同发布配置；模型、fallback、Workflow 超时及候选能力等影响发布指纹的配置必须一致。

## 初始化与启动

项目尚未上线，迁移只有当前初始版本 `0001_initial`。选用新的空开发库执行：

```bash
.venv/bin/alembic upgrade head
```

BFF 就绪后直接受理，无数据库部署门闩。BFF 配置示例见 [bff-run-control.env.example](../../config/environments/bff-run-control.env.example)，启动入口为 `financeclaw.bff.bootstrap:create_default_app --factory` 或 `main:app`。后台生命周期循环随 BFF 启停。

Agent Server 使用 [langgraph.json](../../langgraph.json)，只注册 `finance_agent_v1_5_0`。Worker 是内部子图。部署时将固定 Webhook 域名、允许端口与 HTTPS 策略调整为部署内地址；Agent Server 的 `LG_WEBHOOK_BFF_TOKEN` 与 BFF 的 `FINANCECLAW_BFF_WEBHOOK_TOKEN` 必须相同。模型和产品 API 无权指定回调地址。

BFF Webhook 路径为 `/internal/webhooks/langgraph/{backend_instance_id}`。它认证并限制 64 KiB，只持久化用于唤醒观察的最小事实；正文不能直接成为助手答案。持久化失败返回 503。漏回调时后台仍按 `BFF_RUN_RECONCILE_SECONDS` 核对到期根。

## 执行与观察

- `POST /v1/conversations/{id}/turns` 原子写入 user Journal、Turn、冻结发布、授权、start operation、Inbox 与进度，返回 202；不等待模型输出。
- `POST /v1/interactions/{id}/responses` 绑定原 attempt、checkpoint、interrupt、revision、schema 和动作摘要。人工回答生成一个固定 resume 操作；子图正常完成直接回到顶层 ReAct。
- `POST /v1/runs/{id}/cancel` 接受取消意图，精确尝试停止后才落库 cancelled。客户端断线不会取消执行。
- 查询与 SSE 只读，不触发运行。SSE 重放持久进度与最终 Journal 快照，不承诺 token 重放。
- 多副本使用数据库租约、epoch、唯一 operation 领取和根事务锁，过期持有者不能提交结果。
- `/ready` 分别检查业务库、制品库、Agent Server、BFF 执行生命周期及已启用飞书的内置通知发送器。

常见持久 `waiting_reason`：

| 原因 | 含义与处理 |
|---|---|
| `user_interaction` | 当前原生等待点已登记，回答展示的交互实例 |
| `resume_pending` | 回答已持久化，原 resume 尚待提交／绑定 |
| `submission_uncertain` | 曾取得发送权但回执未知；仅按原 thread＋operation metadata 查找，空结果不会自动重发 |
| `authorization_required` | 新派发所需授权已过期／撤销；显式有限重授权或取消，不能靠 GET 扩权 |
| `interaction_expired` | 问题／审批过期；不能延长原等待点后重新批准，可取消并开启新 Turn |
| `reconciliation_required` | 发布或原生证据不匹配、预算不足、数据库／后端异常等；结合 `last_error` 类别与固定操作排查，不更换 ID 重发 |

授权失效不会妨碍读取已产生的精确完成事实。拒绝意图先入账，原生图处理拒绝后在继续 ReAct 前封闭本根后续副作用；BFF 不提前阻断需要重入的复合 Tool。最终成功须有当前 attempt 的最终 checkpoint、无待运行节点／中断和当前 Turn 的最终 AI 文本；观察事实、assistant Journal、Turn、进度、审计和通知意图共用一次事务。

失败或取消后的下一轮使用干净原生 thread，从 Journal 获取历史上下文，避免执行残留 checkpoint。历史业务记录不因此删除。

## 隔离验证

```bash
.venv/bin/python -m experiments.stage8_hotfix.hf2_native --report /tmp/bff-native.json
.venv/bin/python -m experiments.stage8_hotfix.hf2_native --scenario hitl --report /tmp/bff-hitl-native.json
```

探针使用合成输入、临时 SQLite 业务库和独占本机端口，启动并关闭自己的 BFF 与 LangGraph dev。验证单根子图、连续问题、Workflow 与原生 HITL、重复回调、BFF 重启、断线后的最终 Journal。

此探针使用开发 Agent Server，不证明生产执行服务崩溃后的持久恢复或外部副作用 exactly-once。生产 Agent Server 必须配置持久队列与 checkpoint；无法证明是否已提交的操作保留待核对状态。
