# 记忆与历史索引 Outbox 排查

`outbox_events` 保存需要后台完成的意图。`published` 表示该事件处理完成，不表示“向用户发布了一条消息”，也不表示错误。SQL 是业务与记忆的事实源，Store 是可重建的检索投影；索引故障不等于业务原文丢失。

## 先找负责进程

| destination | 消费角色 | 任务内容 |
|---|---|---|
| `history_index` | integrations | 为已完成问答建立历史检索索引 |
| `memory_extract` | memory_worker | 从有有效来源许可的原文提取记忆候选 |
| `memory_consolidate` | memory_worker | 将提取结果与现有记忆整理、去重或提出候选 |
| `memory_index` | integrations | 将当前有效的 SQL task 记忆投影到 Store |
| `memory_index_delete` | integrations | 删除精确旧版本，核对物化清理 |

检查容器与消费者，再检查任务数量。以下操作只查询状态，不读取来源正文：

```bash
docker compose ps -a
docker compose logs --tail=100 integrations memory_worker
docker compose exec memory_worker python deploy/memory_worker_entrypoint.py operations status

docker compose exec -T postgres psql -U financeclaw -d financeclaw_app -c \
  'SELECT destination, status, count(*) AS events, min(available_at) AS oldest_available_at, max(published_at) AS last_completed_at FROM outbox_events GROUP BY destination, status ORDER BY destination, status;'
```

`operations status` 覆盖四种 `memory_*` 任务，不包含 `history_index`；它输出最老任务、最近完成时间、预算、版本冲突和 `purge_pending` 数量。`[]` 表示没有对应记忆事件，不是查询失败。日志对外分享前仍应脱敏。

## 判断状态与失败原因

| 状态 / 现象 | 含义与下一步 |
|---|---|
| `pending` 持续增长 | 检查负责角色是否存活、事件可领取时间和下游连接 |
| `publishing` | 已被领取；结合租约和进程健康判断是否需要等待接管，不手工抢写状态 |
| `published` | 此事件处理完成；是否真的产生记忆还取决于来源许可、提取结果及候选决定 |
| `dead_letter` | 明确失败或重试耗尽；先看错误类别与处理元数据，再修复依赖或配置 |
| `ModelBudgetExhausted` | 持久模型预算耗尽；查看先前失败和用量记录，不能只凭最终错误判断根因 |
| 模型 / pipeline 版本冲突 | 发布档案与任务快照不匹配；不能静默换模型接管旧来源 |
| 向量维度不一致 | 核对模型真实输出、`FINANCECLAW_EMBEDDING_DIMENSIONS`、`langgraph.json` 和原生 Store 列 |
| `purge_pending` | SQL 已阻止可见性，但外部索引删除或未知写入尚未核对完成 |

记忆提取与整理的模型名、思考模式、超时、输出预算均属于冻结档案的一部分。进程重启不会清零预算，也不会延长来源许可。记忆模型的配置见[模型手册](model-configuration.md)。

## 修复后怎样恢复

`replay` 只支持仍在死信状态、模型 / pipeline 版本匹配且来源仍有效的提取或整理任务。它创建带审计的新事件，保留原失败事件；默认继承剩余模型预算。以下是模板，须先明确目标与重处理范围：

```bash
docker compose exec memory_worker python deploy/memory_worker_entrypoint.py operations replay \
  EVENT_ID --operator OPERATOR --reason '依赖修复后的受控重放'
```

只有明确决定授予新模型预算时才追加 `--new-model-budget`。该参数不能越过已撤销或过期的来源许可，也不能绕过模型指纹变化。不要直接清零 `attempts` / `model_budget`，或把所有死信改成 pending。

索引重建使用[上下文运维](context-budget.md)中的专门命令：记忆 `reindex` 从有效 SQL 事实入队；历史 `history` 默认预览，追加 `--apply` 才写入。两者都可能使被授权内容再次进入 embedding 服务，应按既定数据处理范围操作。没有支持的通用“强制重放所有事件”命令。

以下保留原日期的故障与验收事实，便于理解错误模式；它们不是本次文档整理重新执行的结果，也不代表当前本机服务、数据库或死信数量。

## 历史案例：2026-09-16 本地故障定位

- 部署前复核有 11 条 `history_index` 死信：10 条向量维度不匹配，1 条是之前缺少
  `conversation_messages.skill_access_refs` 的残留（字段补齐见 `skills.md`）。
- 原配置和原生 Store 列为 1536 维；实际 embedding 服务用合成字符串测试返回 1024 维。
  检查时 `store`、`store_vectors` 均为空。应用拒绝错误维度，历史消息未成功建立检索索引。
- 检查时有 8 条 `memory_extract` 死信，最终异常均为 `ModelBudgetExhausted`。
  每个分片已预留 2 次调用、共 4000 输出 token；最终错误表示尝试次数耗尽。
  当时实现没有保留此前异常或 SDK 截断用量，不能断言所有旧任务都以同一原因失败。
- 使用四条完全合成的偏好文本，在当前 Qwen 配置上复现 `LengthFinishReasonError`：
  输入 669 token、输出 2000 token，其中思考 2000 token，未完成结构化 JSON。
  新代码只为记忆任务关闭 Qwen 思考，保持来源许可的输出预算，并保存失败历史和截断用量。

修复后的真实供应商探测使用相同四条合成偏好文本，得到有效结构化结果：输入 661 token、
输出 244 token，正常结束，仍保留 2000 token 上限。合成文本没有登记为真实用户来源或持久记忆。
API 与记忆 Worker 的提取/整理模型指纹实测一致，两阶段分别保留 2000 / 4000 输出上限。
Qwen 非思考结构化输出参数参考[阿里云说明](https://help.aliyun.com/zh/model-studio/qwen-structured-output)。

### 该次空索引维度修复

模型实际输出、`FINANCECLAW_EMBEDDING_DIMENSIONS`、`langgraph.json` 的
`store.index.dims` 与数据库向量列必须相等。修改模型参数不会自动迁移原生 Store 列。
不能靠截断、补零或清空表绕过维度错误。

该次修复采用 1024 维；当前仓库 `langgraph.json` 与设置默认值也为 1024。首次部署直接按该配置建表；已有空的 1536 维表可在索引写入停止后执行：

```bash
docker compose exec -T postgres psql -U financeclaw -d financeclaw_native -v ON_ERROR_STOP=1 < deploy/postgres/store-dimensions-1024.sql
```

脚本在事务锁内复验旧类型与向量为空；非空拒绝执行，已为 1024 时幂等。
有向量的部署需要另行制定重建方案。本地 `.env` 也须同步为 1024，之后重建并统一重启应用镜像。
验证用合成文本执行 Store 写入、语义检索、删除，区分合成探测与真实历史重建。

本地实际迁移已完成，列类型为 `vector(1024)`。已通过真实 HTTP Store 的合成文本写入、
语义检索和删除；验证后无合成条目残留。迁移脚本另外在临时 PostgreSQL schema 中验证了空表
迁移、重复执行和非空拒绝保护；四个应用进程均已更新为相同修复源码。

该次完整自动化回归为 `916 passed, 12 skipped`，Ruff、格式检查、编译和 diff 空白检查通过。
六个常驻服务健康，API `/v1/health/ready` 返回 `ready: true`。这些证据覆盖自动化测试和真实
供应商/Store 的合成探测，没有声称已完成真实 Feishu 用户任务或旧死信原文的端到端重放。

### 该次遗留任务的处理边界

修复消费者不会自动恢复死信。不要直接清零 `attempts`、`model_budget` 或把死信批量改成 pending。
记忆任务绑定来源许可、模型指纹与持久预算；更改思考模式会更新指纹，旧任务不能静默换参数执行。
需要重处理时按现有审计与重新授权流程创建新的任务，保留原失败任务。

该次未重新发送真实历史来源给模型或 embedding 服务。自动审批曾拒绝将失败记忆任务的原文
再次发送给外部模型；实际诊断使用合成数据。旧死信的原文重放和历史索引重建需明确授权后执行。
