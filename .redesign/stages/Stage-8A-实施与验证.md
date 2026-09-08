# Stage 8A 实施与验证

日期：2026-09-08。基线：`41d2a04`（8.0 已提交远程）。

**8A 基础服务已实现，真实 PostgreSQL 与 LangGraph dev 闭环通过。**
自托管 PostgreSQL／Redis Agent runtime 的许可注入验收待授权；8B 可靠主动通知、
8C 旧根接管与生产发布尚未完成。8.0 的历史记录和证据保持不变。

## 正式交付

| 职责 | 代码与行为 |
|---|---|
| 同库受理 | `coordination/application/admission.py`：Turn、用户 Journal、执行快照、有限 grant、固定命令、Inbox、根投影、Audit／Outbox 一次提交 |
| 用户决定 | `application/decisions.py`：原主体、scope、revision、Schema、action hash、期限、Workflow 审批镜像、幂等响应与恢复操作同事务 |
| 独立 Worker | `worker/__main__.py`、`application/coordinator.py`：短事务领取、续租、fencing、有限推进、异常退避、精确回执核对、重启恢复 |
| 认证 Ingress | `ingress/app.py`：先认证后解析，64 KiB 限制，Inbox 提交后才确认，未知尝试待关联和容量限制 |
| Backend Adapter | `backends/langgraph_backend.py`：真实原生 start/resume Webhook、精确 run/checkpoint/interrupt 观察、原 operation 查询、取消确认与应用证据 |
| 领域组合事务 | `application/transitions.py`：显式 Delegation、child 交互、Workflow 输出／拒绝、原 parent 交付与最终 Journal 复用原领域仓储 |
| 持久协调责任 | `repository.py`、`shared/execution_ledger/coordination_tables.py`：七张新增表；未知回调最多 1024 条，每轮补关联最多 100 条 |
| 执行端授权 | `shared/execution_ledger/authorization.py`：有限授权、撤销与范围限制；原预算中间件在模型／工具边界持续复验 |
| 产品接线 | BFF 只受理／纯读；HTTP 与飞书共用响应，支持显式续期与本地撤销；SSE 保持原完成／交互事件格式 |
| 迁移与启动 | `0009_stage8a`、`langgraph.coordination.json`、`compose.coordination.yml`、环境片段与运维说明 |

不引入 Temporal。后台状态保存在 PostgreSQL，协调增强继续在 `coordination` 实现。
框架原生状态不进入核心协调协议；正式默认 backend 为固定实例的 LangGraph。
其他 backend 可接六项 Port 并接受能力门禁，当前没有宣称第二个真实 backend 已接入。

## 已验证场景

[`native-services.json`](../evidence/stage8a/native-services.json) 使用 PostgreSQL 16、
独立 BFF／Ingress、两个 OS Worker，以及 LangGraph API 0.13.3 的 dev/inmem runtime。
BFF 在每次创建／用户决定的 POST 后退出，推进期间没有产品 GET 或 SSE。

| 场景 | 原生操作数 | 结果 |
|---|---:|---|
| 根任务 | 1 | 独立 Worker 写入唯一最终 Journal |
| root → child 提问 → child → root | 4 | 原 child owner 恢复，两项应用证据，最终仅一条助手消息 |
| 全部回调丢失 | 4 | 0 条持久化回调，后台补偿仍闭环 |
| start 和 resume 回执全部丢失 | 4 | 只按原操作查证；真实远端 metadata 无重复操作 |
| 两个 Worker 被终止后重启 | 4 | 从同一库继续原任务，无重复 child／Journal |
| 子任务等待时取消 | 2 | 原尝试全部确认，根 cancelled，不生成助手终态消息 |
| Workflow 批准 | 4 | 原审批恢复、Artifact 与固定 Workflow 输出回到父图 |
| Workflow 拒绝 | 4 | child.execution_status 保留 rejected，父图接收原结果，副作用拒绝标记保留 |

Workflow 映射从完整 checkpoint State 中只提取发布 output_schema 字段，并验证业务 run 与输入 hash。
拒绝后的原委派工具重入必须匹配持久 request、continuation、已领取响应、原 child 与前驱，
不能以“交付结果”为理由允许新的委派或写操作。

`test_coordinator.py` 与 `test_coordinator_edges.py` 的 18 项测试在真实 PostgreSQL 上通过，
包含四个独立进程竞争同一根，仅一个操作领取和一次预算扣减；还覆盖事务回滚、租约过期、
新 wake 不丢失、早到回调关联、撤销／续期、未知提交取消、交付证据缺失、HTTP/SSE 纯读、
兼容驱动拒绝协调根、子运行身份／输出纯读和有事实时禁止 downgrade。
常规回归为 293 passed、8 skipped、2 deselected；18 项 PostgreSQL 测试全部通过。结果另存
[`verification.json`](../evidence/stage8a/verification.json)。

## 自托管运行时的剩余验收

本机现有 `financeclaw-langgraph-api:latest` 镜像为 API 0.13.4、PostgreSQL runtime。
已连接本次独占 PostgreSQL／Redis 并完成框架迁移；未注入真实凭据的启动在许可校验处退出。
随后尝试使用项目本地配置中的许可项，被自动审批审查拒绝：需要明确授权将该凭据注入
选定镜像，该容器可能与 LangSmith 通信。此验证当前没有通过，等待用户确认后运行。

测试支持 `--agent-image`、`--redis-url` 和 `--license-env`；保持原样许可检查，
不以 dev/inmem 成功替代生产运行时成功。拒绝原因和范围记录在
[`self-hosted-runtime.json`](../evidence/stage8a/self-hosted-runtime.json)。

## 启用与回滚边界

入口与操作说明见 [Coordinator 运维说明](../../docs/operations/coordinator.md)，
复现命令见 [`experiments/stage8a/README.md`](../../experiments/stage8a/README.md)。

- 开关默认关闭；开启时仅给新根分配 Coordinator。旧根显示需要迁移，不自动接管。
- 新模式下兼容执行服务拒绝协调根；内部直连 Tool／Workflow 要求改用 Conversation Turn。
- 执行账本的 server_run_id 在新模式下保存 BackendAttempt 索引键，原生 ID 留在 adapter binding。
- BFF、Coordinator 与 Agent Server 使用兼容代码和相同业务库／发布配置；不能混入旧二进制。
- 任何新表有事实时禁止自动 downgrade；回滚必须先处理根并显式归档／迁移。
- 原生出站回调在 API 0.13.3 不能启用有缺陷的 allowed_fields 配置，入口仍会接收完整 body 后最小化存储。
  仅使用受信任内部目标；回调超大或丢失由后台补偿。
- 飞书当前为既有尽力展示，不承诺 BFF 退出后的可靠主动通知；该能力属于 8B。
- 当前没有完成旧根接管、滚动发布、生产容量或 IdP 撤销同步，不宣称 8C 完成。
