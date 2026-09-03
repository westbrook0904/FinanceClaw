# Stage 4：Published Workflows 验证记录

> 2026-09-03 接口修订：本记录中的 Workflow 直连路由是 Stage-4 当时的交付证据；当前产品 OpenAPI 已改为顶层 Agent 的 message-only Conversation/Turn 入口，直连路由仅供 `internal:invoke` 服务身份兼容使用。

更新时间：2026-09-03

状态：**GO（本地验收完成）**。`portfolio_review@1.0.0` 已作为代码发布的固定 LangGraph
Workflow 完成目录注册、真实 Agent Server 执行、durable interrupt/resume、业务持久化、报告制品、
版本固定、幂等和永久 Audit 验证。外部 PostgreSQL/Redis、LangSmith 在线数据集和 OTel 后端仍属于
凭据/基础设施门禁，本次没有用 mock 结果冒充在线证据。

## 1. 包边界与首个业务流程

- `financeclaw.modules.workflows.models`：发布定义、Tool/审批/超时策略和持久化业务事实；
- `financeclaw.modules.workflows.catalog`：进程启动时构造的只读版本目录，只让 active 发布接收新流量；
- `financeclaw.modules.workflows.repository` / `tables`：维护业务 run、Agent Server thread/run、版本、输入 hash、
  输出、artifact 和审批决定的永久映射；
- `financeclaw.orchestration.graphs.workflows.portfolio_review_v1`：唯一 Stage-4 固定图，步骤为严格输入规范化 →
  行情读取 → 数据新鲜度检查 → 集中度/风险计算 → 发布审批 → 报告制品；
- `financeclaw.application.workflow_service`：只做 owner/scope/幂等/超时/版本与 Agent Server 映射，
  不实现第二套 checkpoint、queue、scheduler 或执行引擎；
- `financeclaw.interfaces.http`：新增 `POST /v1/workflows/{workflow_id}/runs`，并支持显式 `WorkflowTarget`；
  普通 Agent、Conversation 和 Direct Tool 路由保持独立。

`langgraph.json` 将 `portfolio_review_v1` 显式映射到发布图。`WorkflowDefinition` 固定了
`workflow_id + version`、assistant/graph revision、Pydantic 输入输出、ModelProfile、精确 Tool 版本、
审批点、超时和状态。没有 Workflow DSL、运行时插件发现或 LLM 动态 DAG。

## 2. 数据、安全、审批与幂等不变量

- 输入在 BFF 和图入口分别按同一 Pydantic schema 验证，规范化后生成稳定 `arguments_hash`；
- `tenant_id + workflow_id + version + client_idempotency_key` 唯一，重复请求返回原业务 run；
- 每个 run 使用独立 UUID thread；业务库固定保存 assistant、deployment revision、ModelProfile 和
  workflow version，恢复时不会解析到最新版本；
- 行情节点只调用 `market_snapshot@1.0.0`，逐项保存 provider、`as_of`、Tool 输入 hash 和版本；
- State 最多保存 20 个受 schema 约束的仓位/快照及报告 artifact 引用，不保存报告正文或凭据；
- 只读瞬时故障由 LangGraph `RetryPolicy` 有界重试，报告写入使用 run/审批点/node 级稳定幂等键；
- 审批前校验 owner、`workflows:approve`、过期时间、允许的决定、发布审批点和原输入 hash；
- Agent Server 返回的 workflow/version/run/hash、审批动作、scope 和 decisions 必须与发布定义一致；
- start、interrupt、approve/reject、complete/fail 均写永久 Audit；终态不可被后续对账改写。

## 3. 自动化验证

```text
Stage-4 tests → 10 passed
repository tests → 76 passed, 4 skipped
Ruff check → All checks passed
Ruff format → 132 files already formatted
compileall → passed
```

测试覆盖固定节点拓扑、Catalog 不可变性、Schema/Tool pin、行情 provenance、新鲜度失败分支、
瞬时故障重试、未授权工具拒绝、interrupt、approve/reject/timeout、报告幂等、租户隔离、请求幂等、
跨 `WorkflowService` 实例恢复、旧发布活动 run 固定、HTTP 合约、Audit 和旧 Planner/DAG Runtime 删除。

4 个跳过项沿用既有环境门禁：Stage-0 Provider/PostgreSQL/Redis 探测和 Stage-3 PostgresStore 重连；
Stage-4 的全部确定性测试均执行。

## 4. 迁移与发布包

Alembic 在独立 SQLite 上实际执行 `upgrade head → downgrade base → upgrade head`。最终 10 张表
（含 `alembic_version`），新增 `workflow_runs` 与 `workflow_approvals`；数据库检查确认发布版本、
thread/server run、hash、artifact、审批人和过期字段，以及四列发布级幂等唯一约束。

隔离构建实际产出：

```text
dist/financeclaw-0.1.0.tar.gz
dist/financeclaw-0.1.0-py3-none-any.whl
```

wheel 检查确认包含 `financeclaw.modules.workflows`、`financeclaw.orchestration.graphs.workflows` 和
`0003_stage4_workflows.py`。

## 5. 真实 Agent Server 验证

使用 Python 3.13、`langgraph dev` 0.13.3、离线模型、真实 SDK HTTP 调用和独立业务 SQLite 执行：

```json
{
  "approved": true,
  "audit_count": 8,
  "interrupted": true,
  "recovered_status": "completed",
  "workflow_version": "1.0.0"
}
```

该验证实际加载 `portfolio_review_v1` graph，创建独立 Agent Server thread，读取两项行情并停在
durable publication interrupt；BFF 校验审批后恢复原发布，写入一个确定性 artifact，随后重建业务
repository/service 并从数据库得到 `completed`。完整输出还返回了实际 run/thread/artifact ID，验证后
临时 Agent Server 已正常关闭。

## 6. LangSmith 回归入口

`financeclaw.operations.workflow_eval_seed` 提供 5 个幂等样本：正常发布、过期数据分支、Tool
瞬时故障恢复、拒绝审批和 checkpoint 恢复。设置 `LANGSMITH_API_KEY` 后可写入
`financeclaw-stage4-published-workflow-v1`；当前验收只验证本地样本契约与节点/Tool trace，没有创建
远程数据集。

## 7. 删除与后续边界

生产包和 `financeclaw` 源码均不存在 `PlanDraft`、`ExecutionPlan`、`DAGBuilder`、`NodeProvider`，也未
重新引入 `harness-planning`、`harness-execution` 或 `harness-runtime`。工作区中用户本地未跟踪目录
不属于生产依赖且未被本阶段修改或提交。

Stage-5 继续处理生产 OIDC、PostgreSQL/Redis 部署配置、对象存储、OTel 后端、告警和剩余兼容包；
Stage-4 不扩展成通用编排平台。

## 8. 顶层 Agent handoff 闭环补充验证

2026-09-03 按对外接口修订完成了 Stage-4 的产品入口闭环：

- active Workflow 和显式 `delegatable` 领域 Agent 被包装为受治理的 delegation Tool；
- delegation Tool 通过 LangGraph `interrupt` 产生严格的 `WorkflowHandoff` 或 `AgentHandoff`，
  Tool 在 child 结束前不会伪造同步结果；
- BFF 按 owner、scope、Catalog、目标版本和 Pydantic Schema 重新验证 handoff，随后创建独立
  child thread/run；
- 新增 `delegations` 表和 Alembic `0004_delegations`，永久保存 parent turn/run、child
  thread/run/server run、目标版本、规范化参数及 hash、授权决定、策略版本、状态和结果；
- child Workflow 审批由父 run 的恢复接口转交既有 Workflow interrupt/resume，父 Agent 无权自动批准；
- child 终态被封装为 `DelegationResult` 恢复原 delegation Tool，再由顶层 Agent 生成会话答案；
- 首个 `market_research_agent@1.0.0` 只持有行情读取 Tool，不拥有根 Conversation，也不能继续委派。

确定性测试覆盖 typed interrupt/resume、Workflow handoff 二次校验、父子 run/thread 隔离、BFF
重建后的映射恢复、child 结果回送、Audit 生命周期和迁移列约束。当前结果为 Stage-4 `19 passed`、
全仓 `85 passed, 4 skipped`；Ruff、compileall、Alembic `upgrade → downgrade → upgrade` 和隔离 wheel
内容检查均通过。4 个跳过项仍是原有外部凭据/基础设施门禁。
