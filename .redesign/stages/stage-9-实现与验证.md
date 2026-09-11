# Stage 9：上下文与记忆实现与验证

2026-09-11。按 [v0.3 实施方案](stage-9-上下文与记忆优化实施方案.md)完成代码替换；原生机制、隔离数据库和故障恢复已验证。真实 embedding 供应商尚未确定，中文语义质量、真实摘要质量与规模性能验收仍待部署配置后的测量，不能把合成模型结果当作这些指标通过。

根发布为 `finance_agent@1.6.0` / `finance_agent_v1_6_0`，部署修订 `context-memory/1`。当前不注册旧根、不保留旧 Journal 上下文策略。未自动重建开发者既有数据库或转换旧 checkpoint；新空库使用唯一 `0001_initial`。

## 已实现的调用链

```text
BFF 冻结真实用户消息 ID、Turn 和执行上下文
  → 新 thread 一次性加载有限的已完成问答
  → LangGraph 原生 messages / 工作摘要
  → 画像按字段直接读取；事件按用户 Turn 初始召回一次
  → 平台工具归档 + 原生请求上下文编辑
  → 每次真实模型尝试的最终预算与 Manifest
  → 原生工具执行 / 必要时一次 HITL / 同根 resume
  → BFF 核验完成，事务写 Journal 和 history_index outbox
```

模型正常循环不再全量读取 Journal。摘要是原生消息 state 的组成部分，不由 `ModelCallLimitMiddleware` 注入历史；该中间件继续负责框架调用限额。摘要模型的实际尝试另经根执行预算与 Manifest 计量。

## 模块边界与框架复用

| 模块 | 责任 | 复用能力 |
|---|---|---|
| `kernel/turns.py`、`context/turns.py` | 统一真实用户锚点、排除合成摘要，校验 BFF 冻结来源 | LangGraph 消息 state |
| `context/compaction.py`、`state.py` | 一次 bootstrap；动态保护当前完整 Turn 和最近 4 Turn；摘要移除前归档 | 公开 `SummarizationMiddleware`、`keep`、`RemoveMessage`、消息 reducer |
| `context/budget.py`、`summary_model.py` | 近似容量统计与摘要实际尝试计量；失败保留原 state | 原生模型接口与有限重试 |
| `middleware/final_context.py` | 完整 system/messages/tools/response schema 的硬预算和每次尝试证据 | 原生请求中间件；位于 retry/fallback 内部 |
| `context/artifacts.py`、工具归档/编辑中间件 | 无注解工具的默认归档、回读引用和结构保护 | `ContextEditingMiddleware` / `ClearToolUsesEdit`、现有 ArtifactService |
| `memory/profiles.py`、`policy.py`、`service.py` | 注册画像字段、明确偏好证据、Store CRUD、幂等回执、状态与有效期 | 原生 Store `get/batch/search/put/delete` |
| `middleware/memory_middleware.py` | 画像确定投影、每 Turn 初始事件召回和 ID 复验 | 可持久化原生 state；空结果、恢复和模型循环复用 |
| `tools/memory.py`、`tools/history.py`、`memory/history.py` | 统一保存、一次必要确认、历史/工件有界回读 | `ToolRuntime`、原生 HITL 与 `Command` |
| `memory/indexing.py`、`worker.py`、`deletion.py` | 确定性历史切块、索引去重、删除恢复 | Agent Server SDK Store API、既有定向 outbox |
| `shared/conversation/lifecycle.py`、`memory/maintenance.py` | 过期工件、归档会话 checkpoint 的预览与受控回收 | 对象存储删除、原生 thread prune API |

删除了 `context/builder.py`、`middleware/context_middleware.py`、`shared/conversation/summaries.py` 及旧领域类型和仓储方法。未建设第二套向量数据库、画像正文表、`memory_heads`、通用任务总线或自动后台画像提取器。提案仅作为当前工具的内部纯策略评估，无独立 candidates 持久化 namespace 或额外确认工具。

## 已固定的行为

- 当前 Turn 保留真实用户问题、工具配对及人工交互。原生摘要只压缩旧 Turn，不覆盖框架私有方法，也不隐藏截断摘要来源。必要内容仍超硬上限时明确失败。
- 小工具结果先留在原生 state；大结果立即归档，小结果清理或被摘要移除前归档。默认规则适用于未标注的 MCP；Skill 只有进入受管工具/产物通道的内容属于平台归档。外部 URL 的归档只保证当时收到的链接。
- 画像使用固定字段 key、`index=False`，直接读取不调用 embedding。事件与历史内容才进入语义索引；普通同 Turn 模型循环不重新查询，显式搜索或记忆变更后的补查单独计量。
- 低风险偏好须由可信用户原文验证为明确、持续表达；模型自报“低风险”不能绕过确认。高影响写入一次原生 HITL；无候选后台自动写入。
- 事件替换、重复撤销及 Store 成功后的审计失败可用同一修改补齐回执。工具返回 `receipt_pending` 时不虚报完成或回滚。删除使用持久任务恢复；旧任务不会删除同字段后来保存的新 mutation。
- 成功遗忘或 Store 已改变但审计未完成时，下一次请求使召回失效，丢弃混合旧摘要及当前 Turn 的旧召回解释；保留当前用户问题和删除回执。Journal、旧 checkpoint 和历史工件的删除是独立范围。
- 历史按完成 Turn 确定性切块，包括长文本尾部。来源 hash、索引版本和 embedding 配置未变化时跳过文档编码；回读复验归属、可见性和 hash。后台不会把历史重新提炼为画像。
- 工件默认保留 30 天；活动 Turn、审批或未对账操作阻止回收。回读目录明确可用、过期、删除状态。原生 state 摘要不代表物理 checkpoint 已回收。

## Schema 与部署

业务 ORM 与初始迁移统一为 **19 张表**：移除 `conversation_summaries`，扩展现有 `artifacts` 的来源/保留字段、`model_context_manifests` 的实际请求证据，以及 `outbox_events.destination/claim_epoch`。不在业务库另建 checkpoint 或 Store 表；这些仍由 Agent Server 管理。

固定已验证版本：LangChain `1.3.18`、LangGraph `1.2.11`、Agent Server `0.13.3`、SDK `0.4.4`。`langgraph.json` 与 `langgraph.local.json` 已声明原生 Store 索引入口；embedding 接口独立配置，维度须同时匹配 Store index。BFF 和 Agent Server 的模型/上下文策略进入发布指纹。

新空业务库执行 `alembic upgrade head`。不要对既有同名 `0001_initial` 的开发库假定 schema 已同步，也不要直接清库。索引 worker、历史重建、删除恢复及工件/checkpoint 回收命令见 [上下文与记忆运维](../../docs/operations/context-budget.md)；完整主体删除见 [数据请求流程](../../docs/operations/data-subject-requests.md)。

## 验证证据

最终测试数量、命令、版本与打包结果见 [机器可读验证记录](../evidence/stage9/verification.json)。测试覆盖原生摘要的同步/异步路径、当前 Turn 保护、工具归档失败、单次 HITL/resume、画像与 101 条事件分离、embedding 次数、事件有效期、历史重建、过期租约、删除/审计故障恢复和空库迁移。

| 验证 | 已证明的范围 | 未证明的范围 |
|---|---|---|
| [原生 HTTP 探针](../evidence/stage9/native-http.json) | 真实 Agent Server HTTP；跨 Turn 工具结果、原生摘要、低风险自动保存、高影响一次审批、同 Turn 查询复用；重启后 state/Store 存活 | 模型输出使用合成模型，不证明真实摘要和检索质量；不等价于生产 HA 部署 |
| [PostgreSQL Store 探针](../evidence/stage9/postgres-store.json) | 独立 schema 下的真实 pgvector/原生 Store；画像 0 次 embedding，索引/查询编码、重建连接；撤销记录退出带状态过滤的语义召回，删除移除物理向量 | `index=False` 本身不会清除已有向量；使用合成向量，不证明中文同义查询质量 |
| PostgreSQL 业务回归 | 随机隔离 schema 内验证运行操作并发/约束；写入前断言真实 `current_schema()` | 未修改或迁移本机既有业务 schema |
| 完整离线行为回归、Ruff、打包检查 | 当前代码与初始迁移；无旧 builder/summary 模块进入 wheel | 跳过或未配置的外部测试不计作通过 |

能力差异已明确处理：Agent Server dev 内存运行时对 `keep_latest` 返回 **422**，因此默认预览后显式报不支持；支持的整线程删除必须指定 `--strategy delete --apply`，不自动降级。未启用全局 thread TTL，也未把生产 checkpoint 的保留能力标为已验证。

PostgreSQL Store `3.1.2` 的 `put(index=False)` 不移除原有向量。服务的所有正常事件搜索都强制 `status=active`，并再次验证业务有效期；撤销/替代保留允许留存的历史证据，退出召回，不能称为物理擦除。需要删除正文与向量时走原生 `Store.delete` 和持久删除恢复任务，探针验证其外键级联删除确实生效。

测试中还发现并修复了原有数据库参数合并问题：连接超时覆盖 DSN 的 `search_path`，使一次旧 PostgreSQL 业务测试误写本机默认 schema。只按本次确切 ID 和合成所有者校验，删除了 4 条合成记录（会话、Turn、用户消息、执行各 1）；既有业务数据和表结构保留。新增离线连接参数回归及真实 schema 前置断言，修复后的隔离测试通过。恢复计数记入机器证据。

## 待配置后的验收

1. 确定 embedding 服务、模型、维度和访问配置；按实际模型完成中文同义查询集及 Recall@6 测量。当前没有提供该服务，`semantic_quality_verified=false`。
2. 用真实摘要模型验证长背景末尾决定、否定和未决问题保真度；当前只验证框架执行与消息完整性。
3. 在目标部署测量 10/100/1000 Turn 的 p95、token 与持久化体积；当前不承诺成本下降比例。生产 checkpoint 保留和 S3 历史版本保留需按部署能力验证。

上述是外部配置与质量测量边界；核心模块、调用链和运维入口已落地，无需恢复旧上下文机制。
