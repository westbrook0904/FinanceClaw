# Stage 2：Conversation Context 验证记录

更新时间：2026-09-02

状态：**GO**。永久 Conversation Journal、token-budgeted Model Context、Summary、Manifest、
Artifact offload 和跨 Agent Server 重启继续会话均已通过；旧 Context Runtime 已删除。

## 1. 已落地能力

- `financeclaw/conversation`：Conversation/Turn/Message/Summary/Manifest 领域记录、SQLAlchemy
  表、append-only repository、确定性摘要与 Model Context 选择；
- `financeclaw/artifacts`：Artifact metadata、local/in-memory store、大 Tool Result 投影、hash
  校验和 owner/scope 读取；
- `financeclaw/application/ConversationService`：Conversation 创建、Profile 固定、幂等 turn、
  Thread/Run 映射、状态补写、resume/stream 与 incomplete turn reconciliation；
- `ConversationContextMiddleware`：每次 model call 组合最近原文、相关摘要/古老原文和当前
  Tool 状态，并永久保存 `ModelContextManifest`；
- development/test 可显式记录脱敏后的最终 system/messages/tools/token budget/Manifest；
  production 对 debug full I/O 保持 fail-fast；
- `alembic.ini` 与 `0001_stage2` migration 管理业务数据库，Agent Server 数据库仍保持独立；
- Agent Server 加载进程级 graph 实例；同步 SQLAlchemy/Artifact 操作从 async 执行路径移入
  worker thread，避免阻塞事件循环。

## 2. 数据与安全不变量

- `(tenant_id, subject_id, conversation_id)` 作为所有权查询路径；跨租户按不存在处理；
- `(tenant_id, subject_id, idempotency_key)`、`run_id`、Conversation message sequence 和
  `model_call_id` 均有唯一约束；
- Conversation 创建时固定 AgentProfile version 和 UUID Agent Server Thread；后续 turn 不可
  切换 Agent/Tool/Profile；
- 用户/Assistant 可见消息仅追加；重新生成通过 `parent_message_id` 追加分支；
- Summary 保留 source IDs、sequence range、model/template versions 和 content hash；重建产生
  replacement 并把旧版本标记为 superseded；
- Context 裁剪记录 omission reason/token count；历史金融内容明确标为可能陈旧；
- Manifest 保存实际使用的 message/summary/tool refs、tool versions、token count 和 context hash；
- 大结果只向模型暴露有界摘要、Artifact reference、hash、source/as-of；读取要求 owner 和
  `artifacts:read` scope；
- Debug I/O 对 credential/secret/reasoning/thinking/hidden 字段执行脱敏。

## 3. 自动化验证

Stage-2 测试覆盖 Journal、Context/Summary、Manifest/debug、Artifact、API/restart/reconciliation
和 Alembic：

```text
13 passed
```

仓库根测试：

```text
52 passed, 3 skipped
```

保留的 Contracts/Events/Memory/Policy/Trace 回归：

```text
29 passed
```

Ruff format/check：

```text
All checks passed
```

依赖锁、环境与 wheel：

```text
uv lock --check → 121 packages resolved
pip check → No broken requirements found
wheel build → financeclaw-0.1.0-py3-none-any.whl
wheel includes Conversation/Artifact modules and Alembic migration; excludes harness_context
```

Alembic 使用临时 SQLite 实际执行：

```text
upgrade head → 6 Stage-2 tables + alembic_version
downgrade base → alembic_version only
upgrade head → schema restored
```

## 4. 真实 Agent Server 重启验证

使用 Python 3.13、`langgraph dev`、offline deterministic model 和同一业务 SQLite，第一轮结果：

```json
{"completed":true,"message_count":2,"manifest_count":2,"agent_profile_version":"1.0.0"}
```

停止并重新启动 Agent Server 后，用同一 `conversation_id` 再执行一轮：

```json
{"completed":true,"message_count":4,"manifest_count":4,"agent_profile_version":"1.0.0"}
```

两次结果的 `conversation_id` 和 UUID `thread_id` 完全相同。该验证证明即使 Server Runtime
重启，Journal 原文、Thread 映射和每次模型调用的 Manifest 仍可继续使用。

## 5. 第二批删除

已删除：

- 完整 `harness-context` 包、测试和 README；
- `ContextItem/Source/Snapshot/Projection/Consumer`、Assembler/Projector/PromptBuilder；
- 旧 Observation Context 投影及契约；
- `PRE_CONTEXT` Policy phase 和对应 engine wiring；
- packaging 中的 `harness_context` entry。

保留的 Memory contracts/policy 只用于 Stage 3，不能重新引入独立 Context DTO/runtime。

## 6. 后续边界

Stage-2 本地默认使用 SQLite/Local Artifact Store；生产配置强制 PostgreSQL、关闭自动建表并由
Alembic 升级。S3-compatible Artifact Store、生产 OIDC/JWT、永久金融 Audit、pgvector 语义召回
和长期 Memory 仍分别属于 Stage 3/5。
