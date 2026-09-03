# Stage 4：发布式工作流实施说明

## 1. 阶段目标

本阶段为真正需要确定步骤、审批、断点恢复和版本治理的业务场景建设少量发布式 LangGraph Workflow。普通开放式请求继续由顶层 ReAct Agent 处理，不为每次请求动态生成 DAG 或复杂 `PlanDraft`。

## 2. 工作流适用边界

只有同时满足以下一项或多项的场景才进入 Workflow：

- 业务步骤固定且需要审计；
- 存在明确的前置条件、分支和失败补偿；
- 必须在人审后继续；
- 运行时间较长，需要断点恢复或异步进度；
- 输入输出契约需要稳定版本；
- 同一流程需要被反复、可预测地执行。

临时分析、探索式问答、单工具调用和由模型自由决定下一步的任务不应被包装成 Workflow。

## 3. 核心设计

### 3.1 WorkflowCatalog

实现不可变、启动期装配的 `WorkflowCatalog`。每个条目至少包含：

| 字段 | 说明 |
|---|---|
| `workflow_id` | 稳定业务标识 |
| `version` | 不可变发布版本 |
| `graph` | 编译后的 LangGraph `StateGraph` |
| `input_schema` / `output_schema` | Pydantic 契约 |
| `model_profile_id` | 固定模型配置引用 |
| `allowed_tools` | 固定工具集合或受控筛选规则 |
| `approval_points` | 人工审批节点定义 |
| `timeout_policy` | 运行级超时策略 |
| `status` | draft、active、deprecated |

Catalog 不做动态发现、运行时插件扫描和 Provider 选择。

### 3.2 首批真实工作流

建议只交付一到两个能覆盖核心机制的业务流程：

- `portfolio_review_v1`：读取组合快照、校验数据时间、执行风险与暴露分析、生成报告；
- `risk_report_v1`：获取受控数据、执行确定性计算、必要时触发审批、发布制品。

最终选择以真实业务价值为准，不为展示框架而新增流程。

### 3.3 图与状态

- 图由代码定义并在发布前编译；
- State 只保存恢复执行所需的结构化字段和引用；
- 大对象写入 Artifact Store，State 只保存 `artifact_id`；
- 节点副作用必须具备幂等键；
- 每个外部结果保存 `source`、`as_of`、输入哈希和版本；
- 敏感凭据永不进入 State、checkpoint 或 trace metadata。

### 3.4 审批与恢复

审批节点使用 LangGraph interrupt/resume：

1. 节点生成结构化 `ApprovalRequest`；
2. 业务 API 校验审批人权限和请求归属；
3. 审批结果永久写入业务 Audit；
4. Agent Server 只接收已验证的恢复命令；
5. 恢复后继续使用原 `workflow_id + version`，不得静默切换版本。

### 3.5 Agent Server 发布映射

- 每个已发布图映射为可部署 graph；
- Assistant 固定引用 graph revision、ModelProfile 和必要配置；
- 一个 Workflow run 使用独立 thread，避免污染用户长期对话 thread；
- BFF 维护业务请求、Workflow thread、run 和制品之间的映射；
- 进度通过 Agent Server SSE 转发，但业务状态以 FinanceClaw 业务日志为准。

## 4. 请求路由

产品会话中的 Workflow 只能由顶层 Agent 通过受治理的 handoff 进入：

- 顶层 Agent 根据用户自然语言，从当前可见的已发布 Workflow 中选择；
- 用户使用 `/workflow <workflow_id>` 表达调用偏好，顶层 Agent 完成参数提取和提槽；
- 产品界面的固定流程操作转换为同等的会话指令，不能直接指定公开 API Target。

模型只能从 Catalog/Profile/Policy 暴露的已发布 Workflow 中选择，不能动态生成 Workflow ID、版本、节点、边和执行策略。服务端在 typed handoff 后再次确定版本、权限和输入 Schema，并创建独立 child run。

## 5. 版本与幂等

- 已开始的运行始终绑定启动时版本；
- 新版本并行发布，旧版本在无活动运行后下线；
- 输入规范化后生成 `arguments_hash`；
- `tenant_id + workflow_id + version + client_idempotency_key` 唯一；
- 节点外部写操作使用 run/node 级幂等键；
- 重放不得重复创建交易、审批或报告制品。

## 6. 迁移与删除

新 Workflow 通过验收后：

- 删除残留的通用 DAG Builder、PlanDraft 生成和动态拓扑校验代码；
- 删除通用 Planner、ExecutionPlan、Node Provider 和相关 SPI；
- 删除旧 planning/execution contracts 中仅服务于自研图运行时的类型；
- 将真正有业务价值的固定流程改写为 LangGraph StateGraph；
- 将旧重试、checkpoint、resume 代码替换为框架配置和少量节点级业务幂等逻辑。

## 7. 测试要求

### 7.1 图结构测试

- 编译后的节点、边、条件分支和 interrupt 点与发布定义一致；
- 输入输出 Schema 在版本内稳定；
- 未授权工具无法进入图的工具集合；
- 图状态不包含凭据和不受控大对象。

### 7.2 恢复与故障测试

- 节点失败后的框架重试符合配置；
- 进程重启后从 checkpoint 恢复；
- 审批前中断、批准、拒绝和超时路径正确；
- Redis、PostgreSQL 或外部金融服务短暂故障后可恢复；
- 同一幂等键不会产生重复副作用；
- 旧版本活动 run 不受新版本发布影响。

### 7.3 观测与评测

- LangSmith 展示完整 Workflow 节点链、模型调用和工具调用；
- OTel 展示 BFF、Agent Server、数据库、Redis 和外部 HTTP 的基础设施链路；
- Audit 可独立回答谁启动、谁审批、使用哪个版本、产生什么制品；
- LangSmith 数据集覆盖正常、分支、工具失败、拒绝审批和恢复场景。

## 8. 验收标准

- 至少一个真实 Workflow 在 Agent Server 上可发布、执行、中断和恢复；
- 所有流程均为代码定义、版本化、可测试的 LangGraph；
- 普通请求不经过 Planner 或动态 DAG；
- 顶层 Agent 能选择或遵循显式 Workflow 指令，BFF 能确定性解析 handoff 并追踪父子 run；
- 幂等、审批、制品、LangSmith trace 和 Audit 形成完整链路；
- 旧 Planner/DAG Runtime 已从生产依赖中移除。

## 9. 明确不做

- 不开发通用 Workflow DSL 或可视化编排器；
- 不让 LLM 动态创建任意图；
- 不建设第二套 checkpoint、queue、run scheduler；
- 不为每个用户请求创建一个固定工作流；
- 不用 Workflow 代替开放式 ReAct Agent。
