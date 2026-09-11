# Stage 9 上下文与记忆运维

近期上下文由 LangGraph state 的 `messages` 和原生工作摘要提供。每个模型请求不再重读 Journal。新 thread 只初始化一次有界的已完成问答；失败或取消后更换 thread 时，不恢复旧审批或未完成工具动作。Journal 保存业务原文，历史检索通过 `search_history` 定位，`read_history`、`read_artifact` 分页回读。

## 容量与摘要

以下环境变量均加 `FINANCECLAW_` 前缀，API 与 Worker 的策略值需要一致，以匹配冻结发布指纹。

| 配置 | 默认 | 作用 |
|---|---:|---|
| `CONTEXT_INPUT_LIMIT` | 800000 | 发布配置的模型容量上界 |
| `CONTEXT_RESERVED_OUTPUT` | 32768 | 回答生成预留，不能小于 `MODEL_MAX_TOKENS` |
| `CONTEXT_SYSTEM_POLICY_RESERVE` | 8192 | 摘要/初始化规划的系统文本预留 |
| `CONTEXT_TOOL_SCHEMA_RESERVE` | 32768 | 摘要/初始化规划的工具定义预留 |
| `CONTEXT_SAFETY_MARGIN` | 32768 | 输入估算余量 |
| `CONTEXT_RECENT_TURNS` | 4 | 摘要时保护的最近已完成 Turn 数 |
| `CONTEXT_SUMMARY_TRIGGER_TOKENS` | 64000 | 消息达到此阈值后尝试摘要旧 Turn |
| `CONTEXT_SOFT_INPUT_TOKENS` | 96000 | 工具结果清理的软目标 |
| `CONTEXT_TOOL_RESULTS_TO_KEEP` | 3 | 原生清理器保留的最近可清理结果数 |
| `SUMMARY_MODEL` | 与主模型一致 | 摘要模型名，复用聊天供应商连接配置 |
| `SUMMARY_MAX_TOKENS` | 4096 | 摘要输出上限 |

`SummarizationMiddleware` 的公开 `keep` 参数按真实用户锚点动态计算；当前 Turn 全部消息受保护。摘要最多 3 次实际尝试（含首次调用），每次实际尝试都计入根执行预算与 `subtype=summary` Manifest。摘要失败保留原 state，硬容量检查仍然生效。

原生 `ContextEditingMiddleware`/`ClearToolUsesEdit` 负责请求投影中的工具清理。平台对所有工具应用同一默认规则：大结果立即归档，小结果在首次清理或摘要移除前归档。MCP 无需额外保留注解；未来通过受管工具通道执行的 Skill 也使用相同规则。只在模型上下文移除内容不会物理删除归档。

最终请求检查覆盖系统提示、工具 schema、消息、画像、事件及结构化输出 schema；位于 fallback/重试内部，按实际尝试保存 `model_context_manifests`。模型提供容量元数据时取更小上界，否则使用发布配置。超限明确失败，不截断用户原文或结构保护结果。

计数为近似值：仅使用本地 `cl100k_base` 缓存，缺失时按 UTF-8 字节保守估算；`TIKTOKEN_CACHE_DIR=""` 强制离线字节路径。Manifest 标记 `token_count_method=estimated`，不能当作供应商结算 token。

## Store 与 embedding

Store namespace 为 `financeclaw/v2/<编码tenant>/<编码subject>/<类别>`。画像在 `profile` 下以字段为 key，原生 `get/batch` 读取、`index=False` 写入；不需要 embedding。长期事件在 `events` 下索引 `content`。原生消息 state 持有当前用户消息 ID 和已召回事件 ID，空结果也缓存；普通工具循环、重试和恢复不会重复初始查询。明确写入/遗忘会使召回失效，显式 `search_memories`/`search_history` 属于额外查询。

真实 embedding 必须独立配置：

- `EMBEDDING_MODEL`、`EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`：OpenAI 兼容 embeddings 服务；密钥只需提供给 Agent Server。
- `EMBEDDING_DIMENSIONS` 默认 1536，必须与所用 `langgraph*.json` 的 `store.index.dims` 一致。
- `EMBEDDING_TIMEOUT_SECONDS` 默认 30；SDK 隐式重试关闭。失败不降级为词法检索。
- 将供应商主机加入 `EGRESS_ALLOWED_HOSTS`；API 只需同一模型名、URL、维度等发布策略，不需 embedding 密钥。

配置文件的 `store.index.embed` 指向 `financeclaw/agent_server/memory/embeddings.py:embeddings`，框架负责文档索引和查询编码。日志以 `embedding method=documents/query` 记录实际方法调用次数、字符量、耗时与失败，`memory_store purpose=...` 区分初始召回、更新后召回、主动搜索和历史索引。PostgreSQL Store 也会用 `embed_documents` 批量编码查询，不能仅凭方法名推断用途；不记录被编码的正文。`OFFLINE_MODEL=true` 使用框架的确定性假向量，仅供机制验证，不能证明中文语义检索质量。

画像不存在通用版本指针表。每字段保存来源、业务版本与本次修改标识，用于回读追踪及同一工具调用重入。明确且持续的低风险偏好自动保存；高影响写入由一次原生 HITL 批准。模型推断不自动生效，未建设后台画像提取进程。

已验证 PostgreSQL Store 的 `index=False` 不会擦除已有向量。因此撤销/替代通过强制 `status=active` 搜索过滤与读取复验退出正常召回；仅需停止使用时保留历史记录。物理删除正文和向量必须执行 `Store.delete`，不能把停止索引当成删除成功。

## 后台索引与删除恢复

完成 Turn 在业务事务内写入 `destination=history_index` 的 outbox 任务，回答不等待 embedding。运行独立后台角色：

```bash
python -m financeclaw.integrations --once
python -m financeclaw.integrations
```

保存或遗忘时若 Store 已成功、审计失败，工具返回 `receipt_pending` 错误并使旧召回失效，不能声称回滚或全部完成。同一修改重入补齐回执；删除还由持久 outbox 恢复。

该进程使用 Agent Server Store API，先处理 `memory_delete` 恢复，再处理历史索引。按来源 hash、索引版本、embedding 服务地址、模型名、维度及离线模式跳过未变化切块，尾部原文也会分块；切块重建移除多余旧块。后台不会写入画像。默认每批 4 个任务、轮询 5 秒，单条最多执行 50 秒，原生 Store 写入成功后 ack 丢失可以重试。旧 claim epoch 不能确认新的租约。

尚未索引的明确 Turn 仍可直接回读。重建可按已认证主体的会话分页预览和入队，返回 `next_offset` 时继续下一页；正在执行的消费者会被跳过，不重放 audit 或 memory_delete：

```bash
python -m financeclaw.integrations.maintenance history \
  --tenant-id TENANT --subject-id SUBJECT --conversation-id CONVERSATION
# 确认范围后追加 --apply；下一页使用返回的 --offset。
```

更换 embedding 服务、模型或维度时先停索引 worker，按框架要求重建向量索引/部署，再重新入队目标历史任务。切勿修改原始问答来触发重建。

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
