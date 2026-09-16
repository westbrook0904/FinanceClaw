# 记忆与历史索引 Outbox 排查

`outbox_events` 同时承载多种异步任务，`published` 表示处理完成，不表示错误。
`history_index` 是对话检索索引；`memory_extract` 是记忆提取，`memory_consolidate` 是整理。
SQL 业务事实为权威来源，Store 是可重建的检索投影。

## 2026-09-16 本地故障定位

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

## 修复空索引的维度

模型实际输出、`FINANCECLAW_EMBEDDING_DIMENSIONS`、`langgraph.json` 的
`store.index.dims` 与数据库向量列必须相等。修改模型参数不会自动迁移原生 Store 列。
不能靠截断、补零或清空表绕过维度错误。

当前模型采用 1024 维。首次部署直接按该配置建表；已有空的 1536 维表可在索引写入停止后执行：

```bash
docker compose exec -T postgres psql -U financeclaw -d financeclaw_native -v ON_ERROR_STOP=1 < deploy/postgres/store-dimensions-1024.sql
```

脚本在事务锁内复验旧类型与向量为空；非空拒绝执行，已为 1024 时幂等。
有向量的部署需要另行制定重建方案。本地 `.env` 也须同步为 1024，之后重建并统一重启应用镜像。
验证用合成文本执行 Store 写入、语义检索、删除，区分合成探测与真实历史重建。

本地实际迁移已完成，列类型为 `vector(1024)`。已通过真实 HTTP Store 的合成文本写入、
语义检索和删除；验证后无合成条目残留。迁移脚本另外在临时 PostgreSQL schema 中验证了空表
迁移、重复执行和非空拒绝保护；四个应用进程均已更新为相同修复源码。

本次完整自动化回归为 `916 passed, 12 skipped`，Ruff、格式检查、编译和 diff 空白检查通过。
六个常驻服务健康，API `/v1/health/ready` 返回 `ready: true`。这些证据覆盖自动化测试和真实
供应商/Store 的合成探测，没有声称已完成真实 Feishu 用户任务或旧死信原文的端到端重放。

## 旧任务处理

修复消费者不会自动恢复死信。不要直接清零 `attempts`、`model_budget` 或把死信批量改成 pending。
记忆任务绑定来源许可、模型指纹与持久预算；更改思考模式会更新指纹，旧任务不能静默换参数执行。
需要重处理时按现有审计与重新授权流程创建新的任务，保留原失败任务。

本次未重新发送真实历史来源给模型或 embedding 服务。自动审批曾拒绝将失败记忆任务的原文
再次发送给外部模型；实际诊断使用合成数据。旧死信的原文重放和历史索引重建需明确授权后执行。
