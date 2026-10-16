# M5b：受理暂停与排空检查

日期：2026-10-15。基线为本地提交 `72e0655`（M6d，未推送）。本阶段补充 M5 的切换准备：当前认证主体可以暂停新 Turn、检查积压，再显式恢复受理。已有任务继续使用 AgentScope ReActAgent、Toolkit、原生 Interaction 与现有 Worker，不新增执行、审批或渠道协议。

这是 Java 服务的受理控制，不是 Python 切换实施。真实模型、MCP、飞书租户、生产恢复和历史数据导入仍待专项验收，紫微普通启用门禁继续保留。

## 行为和边界

| 操作 | 暂停后的行为 |
| --- | --- |
| HTTP 或飞书提交新的 Turn | 返回 `503 / admission_paused`，不生成 Turn、Command 或用户 Journal |
| 原请求以原 key、原内容重试 | 返回原受理回执，不新增任务；同 key 改内容仍是 `409 / idempotency_conflict` |
| 先前已受理的 queued/running Turn | 按现有 Worker 流程继续 |
| 原生审批或澄清答案、取消与恢复 | 保留原有校验和状态恢复流程，不自动批准或重跑不确定副作用 |
| 会话创建、读取、SSE、通知、隐私操作 | 仍可使用；暂停只约束新增 Turn |
| readiness | 数据库与必要表可用时仍为 UP，以便处理已有任务和受理控制 |

范围由现有认证中的 `(tenant, subject)` 决定，不接受请求体伪造 owner。当前部署仍使用一个配置的 Bearer 主体；本阶段没有增加跨租户管理员或生产 IAM。其他主体的任务不会被暂停，也不会出现在当前主体的排空计数中。

受理状态和控制回执由 Flyway V6 建立的 `business.admission`、`business.admission_changes` 保存。新 Turn 的受理事务持有 owner 行共享锁至提交；暂停事务持有同一行排他锁。因此，先获受理锁的提交完成后，暂停才能成功返回；先完成暂停时，新提交被拒绝。不同会话的普通受理仍能并发。数据库锁超时不视为暂停成功，应按原 key 和原请求重试并读取状态。

控制请求使用 revision CAS 和幂等 key，状态更新与回执在同一事务提交。每个新接受的控制请求将 revision 加一；同 key、同请求返回原回执，同 key 改请求或旧 revision 的新请求返回 409。即使已经恢复，再重放旧暂停 key 也只返回旧回执，不会再次暂停；判断当前状态应使用 GET。

飞书仍使用原生适配器。原事件去重和已有 Interaction 答案不受暂停影响；未受理的新消息不会写入 inbox 成功回执，恢复后重投可受理一次。真实飞书平台的重投时限和投递行为尚未验收，不能把这一结果当成消息必达承诺。

## API 与操作示例

所有接口沿用 `Authorization: Bearer $FINANCECLAW_API_TOKEN`：

| 方法与路径 | 返回或请求 |
| --- | --- |
| `GET /v1/operations/admission` | 当前 `{paused, revision}`；无记录时为 false、0 |
| `PUT /v1/operations/admission` | body 只能包含 boolean `paused`、非负整数 `revision`；必须提供 `Idempotency-Key`；返回 `{paused, revision, idempotentReplay}` |
| `GET /v1/operations/drain` | `{scope:"current_owner", admission, counts, drained}` |

在已授权的本地测试部署中，先读取状态再暂停。以下变量保存重试需要的原请求；网络结果不确定时，重复同一个 PUT，不重新生成 key 或修改 revision。

```bash
BASE_URL=http://localhost:8080
REVISION=$(curl --fail --silent "$BASE_URL/v1/operations/admission" \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
PAUSE_KEY=$(python3 -c 'import uuid; print(uuid.uuid4())')
PAUSE_BODY="{\"paused\":true,\"revision\":$REVISION}"
curl --fail --silent -X PUT "$BASE_URL/v1/operations/admission" \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" \
  -H "Idempotency-Key: $PAUSE_KEY" -H 'Content-Type: application/json' -d "$PAUSE_BODY"

curl --fail --silent "$BASE_URL/v1/operations/drain" \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" | python3 -m json.tool
```

让已有任务完成，或通过现有审批、取消和人工核对流程处理积压。准备恢复时再次读取当前 revision，以新 key 提交恢复：

```bash
REVISION=$(curl --fail --silent "$BASE_URL/v1/operations/admission" \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
RESUME_KEY=$(python3 -c 'import uuid; print(uuid.uuid4())')
RESUME_BODY="{\"paused\":false,\"revision\":$REVISION}"
curl --fail --silent -X PUT "$BASE_URL/v1/operations/admission" \
  -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" \
  -H "Idempotency-Key: $RESUME_KEY" -H 'Content-Type: application/json' -d "$RESUME_BODY"
```

409 表示请求冲突或 revision 已变化，应先核对当前状态和原请求。暂停状态是备份中的业务事实，重启、升级或恢复数据库均不会自动开启受理。

## 如何读取排空报告

报告以一个 PostgreSQL 查询获取受理状态和所有计数，使用同一 MVCC 快照。它只读事实，不执行取消、批准、通知确认、记忆删除或不确定工具重试。

| 计数 | 含义与处理入口 |
| --- | --- |
| `queued` / `running` / `waiting` / `cancelling` / `blocked` | 当前主体的非终态 Turn；queued 包括 resuming。沿用任务查询、交互、取消与恢复流程 |
| `active_commands` / `pending_interactions` | 未完成命令、未解决的审批或输入；已回答的历史交互不计入 |
| `uncertain_effects` | 仍为 pending 的工具副作用回执；即使 Turn 已终态，也不能据此认为外部结果已确定 |
| `pending_outbox` | 尚未投影的业务事件，由现有 Integrations 处理 |
| `pending_notifications` | 待发送或发送中的飞书通知 |
| `unverified_notifications` | unknown / submitted_unverified；SDK 未提供可靠送达核对，报告保留该事实，不自动补造确认 |
| `unprojected_notifications` | 当前未静音绑定的等待或终态 Turn，还没有对应 revision 的有效通知记录 |
| `pending_memory_jobs` | 尚未 done / obsolete / forgotten 的记忆任务，包括正在执行及配置/失败后待重试任务 |
| `pending_forget` | 尚未完成隐私清理的 owner 记录 |

只有 `paused=true` 且所有计数为零，`drained` 才是 true。仍开放受理时，即使计数全零也返回 false。计数可能描述同一任务的不同阻塞原因，不能相加当作任务总数。

离线部署未配置记忆提取模型时，普通消息产生的记忆任务会保留积压；应补齐配置并处理原任务。忘记操作仅按用户隐私意图使用，不能作为清空运维指标的手段。原生飞书的 `submitted_unverified` 也会持续阻止“全部已核对”的结论，真实渠道交付需要另行审查框架能力。

`drained` 是当前主体在查询时点的观测，不是全局写入围栏。其他主体仍可运行，隐私或绑定控制仍可产生写入。迁移方案要求的全局消费者停用、唯一写入者、所有主体核对、一致性备份、数据导入和回退演练仍需独立执行，不能仅凭此值切换渠道或数据库。

## 验证与复现

```bash
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/test-live-acceptance.py
python3 scripts/collect-evidence.py
python3 scripts/init-local.py
python3 scripts/validate-compose.py --ziwei --admission
python3 scripts/check-delivery.py
```

Java 汇总位于 `target/m5b/results.json`，容器记录位于 `target/m5b/deployment/compose-results.json`。Compose 使用随机隔离项目和新卷、离线模型、合成数据，禁用真实飞书/MCP；结束后只清理该项目。源文件与 JAR 摘要会写入证据。

测试覆盖数据库暂停/受理锁的两种先后顺序、独立会话并发、并发控制重试、主体隔离、严格认证与输入、原生审批和澄清、取消、不确定副作用、通知和记忆积压。容器实验补充 API 重启后保留暂停、暂停期间 Worker SIGKILL 后原审批继续、积压清空后排空、35 张表完整备份恢复以及还原后显式开启受理。

本阶段完整 Java 回归 **642 项通过**（495 单元/契约、147 集成），零失败、零错误、零跳过，`spotless:check` 通过；其中 M5b 新增 13 项 Admission 集成测试及 1 项飞书连续性测试。另有 **8 项 Python 离线验收器契约测试通过**，不计入 Java 数量。见 [Java 证据与 178 个源码/配置指纹](evidence/m5b-local-results.json)。

同一份 JAR 的 Compose 实验 **14 项检查通过**，包括原部署检查及新增暂停/排空/恢复链路。35 张表的行数和内容 hash 在备份还原后全部一致，受理控制回执也完整保留。12 次离线合成请求全部成功，P95 为 **1.37 秒**；不代表真实模型性能或生产容量。部署输入和 JAR 指纹均与本地 Java 证据匹配，见 [Compose 证据](evidence/m5b-compose-results.json)。随机测试项目和卷已清理；现有部署未切换。

Compose 沿用已有合成数据的忘记实验，再验证隐私清理完成后的排空状态；它没有把“忘记”作为生产排空流程。M5b 已本地提交 `07a9bfb`，未推送。后续 [M5c](m5c-backlog.md) 增加阻塞项的只读分页定位。真实模型、远程 CI、真实飞书租户和线上渠道切换仍待验收，没有解除真实环境门禁。

代码入口：[AdmissionService](../../src/main/java/com/financeclaw/app/operations/AdmissionService.java)、[OperationsQueryService](../../src/main/java/com/financeclaw/app/operations/OperationsQueryService.java)、[OperationsController](../../src/main/java/com/financeclaw/api/OperationsController.java)、[OperationsMapper](../../src/main/java/com/financeclaw/infrastructure/task/OperationsMapper.java)、[V6 迁移](../../src/main/resources/db/migration/V6__admission_control.sql)、[AdmissionIT](../../src/test/java/com/financeclaw/m5/AdmissionIT.java)、[FeishuIT](../../src/test/java/com/financeclaw/m4/FeishuIT.java)。
