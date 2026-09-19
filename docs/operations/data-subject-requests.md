# 用户数据导出、遗忘与删除

先区分请求范围：关闭记忆读取、忘记一条记忆、清理到期数据、导出或删除整个主体的数据，是不同操作。当前产品提供单条记忆管理和受控保留接口；完整主体导出/删除仍是跨系统运维流程，没有模型可调用的批量 `delete_subject_data` 工具。

## 身份与记录

使用认证身份重新核实 tenant、subject 与请求范围，不能只信任正文中的用户 ID。为每个请求记录请求编号、核实后的 owner、涉及系统、对象数量、操作者、异常及完成时间。导出只包含该 owner 的业务数据与必要引用，不混入凭据、其他租户数据或无关 Audit 主体。

当前应用 SQL 保存会话、Journal、记忆事实、来源许可和审计。原生 AgentServer 保存 checkpoint 与 Store 投影，Artifact 保存工具结果和上下文归档；LangSmith、Provider 和运行日志可能持有另外的副本。单次产品 API 调用不会自动清除所有这些系统。

## 当前记忆管理接口

| 用户意图 | 入口 | 实际效果 |
|---|---|---|
| 查看记忆与来源引用 | `GET /v1/memories`、`GET /v1/memories/{memory_id}` | 返回认证 owner 当前可见的 SQL 版本 |
| 关闭运行时读取 / 自动提取 | `PATCH /v1/memory/settings` | 按设置 revision 更新策略；关闭读取不等于删除历史 |
| 忘记一条记忆 | `DELETE /v1/memories/{memory_id}` | 清除可读正文，建立重放屏障，并异步清理索引 |
| 拒绝待确认候选 | `POST /v1/memory/candidates/{candidate_id}/decision` | 决定固定候选内容，不恢复或启动业务 Turn |

写操作要求 `Idempotency-Key`；修改、删除、决定及设置更新使用当前 `expected_revision`。候选决定还需已展示内容的 `content_hash`。归属来自认证身份，权限由 scope 控制；相同键不能换成另一份请求内容。

遗忘以 SQL 事务中的可见性变更和屏障为准，随后由 integrations 清理精确版本的 Store key。`forgotten` 表示正文已不可见；`purge_pending` 表示物化副本尚未完成核对。删除结果未知、外部写仍可能迟到或任务仍失败时，不能报告物理清理完成。旧来源、旧模型输出与幂等重试不能自动重新记住该记录。

每次实际模型请求会检查隐私版本。该机制防止后续请求继续读取已撤回的记忆，但不能收回已经发给 Provider 的内容，也不会自动删除 Journal、旧 checkpoint、Artifact 或既有 trace。旧 Stage 9 文档中的 Store-first `forget_memory(mode="revoke"/"delete")` 不代表当前 SQL 记忆管理流程。

## 整个主体的数据请求

完整删除应在运维流程中按以下顺序组织，具体系统删除命令须依据当前部署和存储保留策略准备：

1. 核实请求范围并停止该主体的新任务受理，结束或对账活动 Turn、审批、通知和未知外部操作。
2. 隐藏或删除受影响的 Journal 来源，撤销记忆派生许可，处理记忆正文与相关派生版本，先阻止旧事件重新生成可见内容。
3. 清理该 owner 的历史 v2 Store namespace 与记忆 v3 索引；按原生 API 清理其执行快照关联的 thread/checkpoint。
4. 核对 Artifact 内容、索引记录和对象存储的历史版本；有保留锁或尚未到期的对象应记录为待办。
5. 单独协调 LangSmith、Provider、日志和备份副本的处理。追加式 Audit 的保留或去标识化遵循部署已确定的政策，不能把它与可变业务正文混为一谈。
6. 对每个系统记录已完成、待处理或例外；只有全部纳入范围的副本完成核对，才能声明整项请求完成。

这些步骤是运维程序，不表示仓库已经提供一键执行器。工具结果中的远程 URL 只是一项引用，Artifact 保存的是当次实际取得的载荷，不代表自动抓取了链接目标全文。

## 保留清理与验收

[上下文与记忆运维](context-budget.md)提供 Artifact 清理和 checkpoint 回收的预览/应用命令。它们只处理符合条件的到期对象或已归档会话，不能替代完整主体删除。生产对象存储的版本策略和备份保留周期也需单独核对。

验收至少记录：后续读取不可见、旧事件重放不恢复正文、Store 清理回执、对象版本状态，以及仍未处理的外部副本。`purge_pending` 的诊断见 [Outbox 手册](memory-outbox.md)。
