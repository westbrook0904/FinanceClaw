# Stage 11 上下文与异步记忆运维

短期执行内容由原生 state/checkpoint 和唯一的 `WorkingContext` 管理；业务 Journal 保留原始问答。长期记忆及画像以应用 PostgreSQL 为事实源，Store 只用于检索 ID，返回正文前必须回读 SQL 的当前可见版本。详细契约见 [Stage 11 方案](../../.redesign/stages/stage-11-异步记忆与上下文治理实施方案.md)。

## 进程与配置

同一镜像运行 `api`、原生 `worker`、`integrations`、`memory_worker`。记忆工作进程不导入 AgentFactory、不使用原生图队列；模型调用、整合和索引不会占用业务图执行槽。以下变量均加 `FINANCECLAW_` 前缀；API 和执行 Worker 的发布策略必须一致。

| 配置 | 默认 | 含义 |
|---|---:|---|
| `MEMORY_ENABLED` | true | 允许记忆写入和运行时召回；关闭仍可遗忘/拒绝 |
| `MEMORY_AUTO_EXTRACT` | true | 允许来源派生和后台提取 |
| `MEMORY_AUTO_COMMIT_LOW_RISK_PREFERENCES` | true | 明确且持续的有限语言/格式偏好可即时写入 |
| `MEMORY_PERMIT_SECONDS` | 86400 | 单来源有限派生许可；重试不延期 |
| `MEMORY_CANDIDATE_SECONDS` | 604800 | 候选有效期 |
| `MEMORY_EXTRACTION_CONCURRENCY` | 2 | 每进程提取并发 |
| `MEMORY_CONSOLIDATION_CONCURRENCY` | 1 | 每进程整合并发；同 owner 仍由 SQL 串行化 |
| `MEMORY_WORKER_LEASE_SECONDS` / `MEMORY_WORKER_RENEW_SECONDS` | 120 / 20 | 任务租约和续租间隔 |
| `MEMORY_MODEL_TIMEOUT_SECONDS` | 45 | 每次后台模型调用超时 |
| `MEMORY_EXTRACTION_MODEL` / `MEMORY_CONSOLIDATION_MODEL` | 主模型 | 分别冻结的结构化模型档案 |
| `MEMORY_MODEL_ALLOWED_DATA_CLASSES` | 全部已知分级 | 后台模型可处理的来源最高分类 |
| `MEMORY_MODEL_ALLOWED_REGIONS` | `["global"]` | 后台模型允许处理的区域 |
| `PROCESSING_REGION` | global | 受理时写入执行快照和来源许可的处理区域 |
| `MEMORY_RECALL_TOKENS` / `MEMORY_RECALL_LIMIT` | 4096 / 6 | 任务检索与目录的有限注入预算 |

来源、模型档案、pipeline、schema、policy 版本在任务中冻结。不同版本的部署不能静默接管旧模型任务；过期、撤销、无输出分别留下正常完成回执。模型调用次数和输入/输出预算在发请求前预留并持久化，重启或重领不重置。

```bash
FINANCECLAW_PROCESS_ROLE=memory_worker .venv/bin/python -m financeclaw.memory_worker
.venv/bin/python -m financeclaw.memory_worker.operations status
.venv/bin/python -m financeclaw.memory_worker.operations replay EVENT_ID --operator OPERATOR --reason REASON
.venv/bin/python -m financeclaw.memory_worker.operations retain
# 查看预览后，在授权范围内执行清理。
.venv/bin/python -m financeclaw.memory_worker.operations retain --apply
```

Compose 中通过专用入口构造同一身份的 DSN，不假定 `docker exec` 会继承 PID 1 动态设置的环境；本机直接运行则须显式设置对应 `FINANCECLAW_DATABASE_URL`：

```bash
docker compose exec memory_worker python deploy/memory_worker_entrypoint.py operations status
```

将 `status` 替换为相应运维子命令即可在容器内操作。索引重建按当前 SQL active task 分页，使用同一 request ID 重试同一页：

```bash
.venv/bin/python -m financeclaw.memory_worker.operations reindex \
  --tenant-id TENANT --subject-id SUBJECT --operator OPERATOR --reason REASON \
  --request-id REBUILD_ID
```

响应包含 `next_after` 时以 `--after` 继续；重建不会增加事实 revision，也不会重建已遗忘或来源已失效的记录。

重放不延长来源许可，默认复用剩余预算；显式 `--new-model-budget` 才创建新预算并记录运维审计。没有自动扫描旧聊天的启动回填。角色健康由心跳与独立消费者存活判断；`status` 提供队列状态、最老任务、成功时间和模型预算，不能仅靠容器存活判断积压已经处理。

## 用户写入、确认与遗忘

`POST/PATCH/DELETE /v1/memories`、`PATCH /v1/memory/settings` 和候选决定均要求 `Idempotency-Key`。编辑、删除、决定使用唯一的 `expected_revision`；相同 key 不同内容返回冲突。owner 和权限来自认证身份。

明确长期偏好在原始输入受理事务中解析；普通问答在最终 Journal 同事务入队后由后台提取、跨会话整合。模型产物只能引用服务端登记的原文，助手答案不能作为用户画像依据。重要字段产生 `proposed` 候选，通过 `/v1/memory/candidates/{id}/decision` 确认。飞书候选卡使用独立消息和回执，不占用或覆盖任务卡，也不创建 resume 命令。

遗忘先清除 SQL 可读正文、建立防重放屏障并撤销相关派生内容，再异步清理精确 Store 版本。`forgotten` 是逻辑可见性结果，`purge_pending` 表示物化副本尚未核对；未知或可能迟到的 HTTP 写不能被报告成物理清理完成。旧用户来源、旧模型产物和旧幂等重试均不能自动重新记住。

## 上下文容量与压缩

`MODEL_CONTEXT_WINDOW_TOKENS`、`MODEL_MAX_INPUT_TOKENS`、`MODEL_MAX_TOKENS` 和 `MODEL_CAPACITIES` 冻结真实模型容量；fallback 使用所有候选的共同最小窗口。输入上限综合独立输入 cap、总窗口减输出预留和应用上限，独立输入 cap 不再重复扣输出。

一个 Turn 冻结 SQL 画像和有限任务目录，普通新任务不强制做 embedding 查询；明确历史延续或 `search_memories` 才检索。Store 不可用或没有有效命中时有界回退 SQL。后台普通更新下一 Turn 生效；遗忘/关闭读取/source 隐藏提高隐私版本，每次真正模型尝试都重新检查。画像和检索内容仍是历史数据，不能成为交易授权、当前行情或可执行指令。

已完成的较早工具批次可压缩；真实用户原文、澄清、未完成的完整调用/结果配对必须保留。先归档唯一原文，再生成受限结构化 WorkingContext，校验后通过公开原生 reducer 一次提交。状态内不重复存摘要正文，不直接改 checkpoint 私有表。相同输入摘要失败最多两次，最终无法容纳必保内容时明确失败并保留原文。

每次实际回答/摘要请求的 Manifest 记录估算器、完整输入容量、模型版本、记忆 ID/revision、owner/隐私版本、摘要来源和可选项省略原因；Provider 返回用量时另记 observed tokens，缺失时留空。离线 tokenizer 无本地缓存时采用 UTF-8 字节保守估算，不进行运行时下载。

## Store 与后台索引

记忆 namespace 为 `financeclaw/v3/<编码 tenant>/<编码 subject>/memory_index/memory-v1`，key 为 `memory_id:revision`；原有问答历史检索保持其独立 v2 namespace。画像直接读 SQL，不做 embedding。当前发布固定 `memory-v1`，更换向量 schema 或 embedding 档案必须通过明确的新索引发布和重建，不接受任意配置版本后静默忽略。

历史索引、记忆索引、删除各有独立消费循环。内容不写入 Outbox；事件只带 owner、ID、revision 和版本，由消费者回读当前 SQL 事实，HTTP 前后都核验。乱序删除只针对旧版本 key。

真实 embedding 仍由原生 Agent Server 配置 `EMBEDDING_MODEL`、`EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_DIMENSIONS`；维度必须与 `langgraph*.json` 一致，供应商主机须位于出站允许列表。离线模型使用确定性假向量，不能证明中文检索质量。

已完成问答历史重建保留现有显式命令：

```bash
.venv/bin/python -m financeclaw.integrations.maintenance history \
  --tenant-id TENANT --subject-id SUBJECT --conversation-id CONVERSATION
```

## 数据保留

工件默认 `ARTIFACT_RETENTION_DAYS=30`。活动 Turn、待处理审批、未对账出站操作会保护所属会话的工件；目录保留过期/删除状态和 hash。未知来源不会自动判定为可删除。

```bash
# 默认仅预览；显式 --apply 才删除已到期且无活动引用的对象内容。
python -m financeclaw.integrations.maintenance artifacts
python -m financeclaw.integrations.maintenance artifacts --apply

# 只允许已归档且业务与原生运行均无待办的会话。
python -m financeclaw.integrations.maintenance checkpoints \
  --tenant-id TENANT --subject-id SUBJECT --conversation-id CONVERSATION
```

checkpoint 回收通过原生 API，默认策略 `keep_latest`。已验证的 dev 内存运行时不支持该策略（422），但支持整线程 `delete`：确需删除归档会话的所有 checkpoint 时显式追加 `--strategy delete --apply`，不会自动从保留最新状态降级为整线程删除。不启用全局 thread TTL，不声称消息摘要等于物理回收。运行时不支持该 API 时明确失败，保留原数据；能力探针结果见 [Stage 9 验收记录](../../.redesign/stages/stage-9-实现与验证.md)。完整数据主体请求按[数据请求流程](data-subject-requests.md)处理，单条记忆遗忘不会删除 Journal、旧 checkpoint 或供应商 trace。
