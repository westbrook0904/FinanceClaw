# Stage 3：Long-term Memory 验证记录

更新时间：2026-09-03

状态：**GO（本地验收完成）**。长期记忆的提议、HITL 确认、LangGraph Store 持久化、
跨会话召回、Manifest/Audit 追踪和旧 Runtime 删除均已通过。外部 PostgresStore 与
LangSmith 在线门禁保留为凭据驱动测试；当前工作区未配置对应 DSN/API Key，因此未重复执行
Stage 0 已完成的外部兼容性探测。

## 1. 包边界与落地能力

- `financeclaw.memory.models`：只承载 `MemoryDraft`、`MemoryRecord`、生命周期、来源与召回引用；
- `financeclaw.memory.policy`：只处理长期记忆内容边界、敏感等级和确认要求，不引入通用规则引擎；
- `financeclaw.memory.service`：作为 LangGraph `BaseStore` 的薄治理层，负责可信 namespace、
  Journal 证据、生命周期、检索投影和 Audit/Trace；
- `financeclaw.memory.tools`：提供 `search_memories`、`propose_memory`、`confirm_memory` 和
  `forget_memory` 四个 `BaseTool`，并统一接入 ToolCatalog/ToolPolicy；
- `financeclaw.agents.memory_middleware`：在模型调用前召回，使用独立 token budget 注入明确的
  data-only 区域，并把实际引用交给 Conversation Context Manifest；
- `financeclaw.audit`：增加永久 SQLAlchemy Audit repository/table，业务数据库使用共享 ORM Base；
- `financeclaw.application`：增加真实 Agent Server memory smoke、LangSmith 回归样本 seed 和
  HITL 过期保护；客户端适配层把新版 Agent Server 的 checkpoint interrupts 归一化为稳定的
  `interrupted` 业务状态；
- Alembic `0002_stage3` 增加 `audit_records` 和 Manifest `memory_refs`，migration 目录显式归入
  `financeclaw.infrastructure.migrations` 包，wheel 不再产生模糊包分类告警。

关键边界均有解释性注释，重点覆盖 namespace 编码、Store 索引/逻辑删除、ToolRuntime 注入、
数据区防提示注入、同步持久化异步卸载和 Agent Server HITL 状态差异。

## 2. 数据、安全与治理不变量

- 模型可生成的 `MemoryDraft` 仅含类型、内容和证据 message IDs；tenant、subject、namespace、
  sensitivity 和 status 由可信 `ExecutionContext` 与系统策略绑定；
- Store namespace 使用 tenant/subject 的无损 URL-safe Base64 标签，调用方不能传入 namespace；
- 每个 proposal 必须解析到当前 owner 的 Conversation Journal，并至少包含一条用户原话；
- 只允许 `preference`、`goal`、`constraint`、`decision_note`，凭据和时效性金融事实被拒绝；
- 写入采用 propose → confirm → persist；`confirm_memory`/`forget_memory` 禁止 Direct Tool 路径，
  只能经过 Agent 的 HITL Middleware；
- superseded/revoked/deleted 记录不再被检索或注入，逻辑记录保留以满足审计；
- 召回内容被标记为历史上下文和纯数据，当前行情、持仓、余额、财报、新闻、利率与产品规则
  始终由受治理工具提供；
- Manifest 保存 `memory_id`、schema version、type 和 injection reason；Audit 只保存内容 hash、
  evidence refs 与敏感等级，不复制记忆正文；
- async Agent Server 链路中的 SQLAlchemy 与 Store 投影工作移到 worker thread，避免阻塞事件循环。

## 3. 自动化验证

Stage-3 确定性与集成测试覆盖模型形状、金融事实/secret 拒绝、namespace、证据、生命周期、
supersede/revoke/delete、跨 conversation、tenant/subject 隔离、Store 内伪造、真实 ToolRuntime
注入、HITL approve/reject/timeout、Prompt/Manifest 一致性、永久 Audit、迁移、Direct Tool 绕过
和 Agent Server 状态归一化。

```text
Stage-3 tests → 14 passed, 1 skipped
repository tests → 66 passed, 4 skipped
Ruff check → All checks passed
Ruff format → 116 files already formatted
```

跳过项均为环境门禁：3 个 Stage-0 provider/PostgreSQL/Redis probe，以及 1 个 Stage-3
PostgresStore 重连测试。Stage-3 Postgres 测试在设置 `FINANCECLAW_SPIKE_POSTGRES_DSN` 后会使用
两个独立 `PostgresStore.from_conn_string(...)` 生命周期验证重启后可读。

Alembic 在独立 SQLite 上实际执行：

```text
upgrade head → downgrade base → upgrade head
最终包含 7 张业务表 + alembic_version；audit_records 和 memory_refs 均存在
```

发布包验证：

```text
uv build → financeclaw-0.1.0.tar.gz + financeclaw-0.1.0-py3-none-any.whl
wheel includes financeclaw.memory and 0002_stage3 migration
wheel excludes harness_memory and harness_policy
```

## 4. 真实 Agent Server 验证

使用 Python 3.13、`langgraph dev`、离线确定性模型和同一业务 SQLite 执行完整链路：

```json
{
  "write_interrupted": true,
  "write_approved": true,
  "cross_thread_recall": true,
  "manifest_count": 2,
  "audit_count": 8,
  "memory_ids_count": 1
}
```

该验证实际经过两个不同 Conversation/Agent Server Thread：第一条偏好先产生 proposal，停在
`confirm_memory` 的 durable interrupt；批准后写入 Store；第二个 thread 召回同一 subject 的偏好，
最终 Assistant 输出、Manifest memory refs 和永久 Audit 均一致。

## 5. LangSmith 回归入口

`financeclaw.application.memory_eval_seed` 提供 5 个幂等样本：稳定偏好召回、新偏好替代旧偏好、
租户隔离、实时工具事实优先、高影响记忆确认。设置 `LANGSMITH_API_KEY` 后可写入
`financeclaw-stage3-memory-regression-v1`；当前工作区无 Key，因此本次只验证了样本契约与 traceable
span 埋点，没有创建远程数据集。

## 6. 第三批删除

已删除完整 `harness-memory`、完整 `harness-policy`、旧 `harness_contracts.memory` 及对应测试、
README 与 packaging entry。生产路径不存在旧 Memory Provider/Gateway、SQLite/InMemory 双实现、
兼容适配、双写或双读。

## 7. 后续边界

Stage-3 不引入独立向量数据库。只有真实数据量证明 PostgresStore/pgvector 无法满足召回质量或
延迟目标时，才按隔离、删除一致性、成本和运维复杂度重新评估；生产 OIDC、S3 Artifact Store、
部署级监控与剩余旧 runtime 清理由 Stage 5 承担。
