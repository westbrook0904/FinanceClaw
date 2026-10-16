# M5e：旧备份隐私核对与隔离演练

日期：2026-10-15。基线为 M5d 本地提交 `993e7ac`，未推送。原恢复实验在遗忘完成之后备份，只能证明该备份保留遗忘状态。M5e 增加“先备份、后遗忘、再恢复旧库”的负向场景，并提供恢复前核对工具。

**恢复库接入 API、Worker、Integrations 或 Memory Worker 之前，先保持隔离并核对最新隐私事实。** 单个主体的 M5d 排空报告不能覆盖全库隐私；M5e 通过管理员显式导出的全主体 SQL 快照比较，输出可复核报告。

## 核对内容

[快照 SQL](../../ops/restore-privacy-snapshot.sql) 在一个 `REPEATABLE READ READ ONLY` 事务中查询 `memory` schema，语句超时为 30 秒，不调用业务 API，不初始化 owner，不读取记忆正文。快照包含主体标识、epoch、待清理标志、遗忘/清理审计的 ID/epoch/时间，以及各类异常行数量。

[比较脚本](../../scripts/check-restore-privacy.py) 要求两份快照满足：

- 主体集合、epoch 和所有 `forget` / `purge_complete` 审计事件一致；同名 subject 的不同 tenant 分别核对。
- 两侧均没有待清理标志或没有 owner 的记忆记录。
- 每次 epoch 增长都有对应遗忘事实，最新 epoch 有其后的清理完成事件。连续遗忘可以合并清理，但不能缺少遗忘事实。
- 旧 epoch 的来源/事实已清空正文，索引/召回没有引用已遗忘版本，旧作业不会再次执行，也不保留执行租约。未来 epoch 和错误的审计 target 同样阻断。

同一 epoch 中被替代偏好的旧索引可以保留，由原查询按状态/revision 过滤；这种情况不属于遗忘恢复违规。容器演练包含该正常场景，避免把常规偏好更新误判为需要隐私修复。

仅修改 epoch 无法通过。一个还原库的隐私状态比参考库“更高”也会阻断，交由人工核对来源；脚本不自行合并两边事实。

## 使用步骤

先停止参考部署的所有写入者，取得**包含备份之后所有遗忘请求的最新权威状态**。单次 SQL 的事务一致性不能阻止导出之后的新请求；应维持停写，或者在恢复前重新取得并核对最新状态。若原库不可访问且没有独立保存的最新隐私事实，保持恢复库隔离，不能把同一份旧备份导出两次充当验收。

以下以两个已经隔离、允许管理访问的 PostgreSQL 连接为例。`PGSERVICE` 由受限的本地 service/passfile 或密钥管理配置，不把密码放入命令参数。`financeclaw-current` 必须指向权威最新库，`financeclaw-restored` 指向尚未接入应用的恢复库。

```bash
umask 077
SNAPSHOT_DIR=$(mktemp -d /tmp/financeclaw-privacy.XXXXXX)
PGSERVICE=financeclaw-current psql -X -qAt -v ON_ERROR_STOP=1 \
  -f ops/restore-privacy-snapshot.sql > "$SNAPSHOT_DIR/reference.json"
PGSERVICE=financeclaw-restored psql -X -qAt -v ON_ERROR_STOP=1 \
  -f ops/restore-privacy-snapshot.sql > "$SNAPSHOT_DIR/restored.json"
python3 scripts/check-restore-privacy.py \
  --reference "$SNAPSHOT_DIR/reference.json" \
  --restored "$SNAPSHOT_DIR/restored.json" \
  --output "$SNAPSHOT_DIR/privacy-report.json"
```

两次 `psql` 都必须成功再运行比较；不要使用允许错误后继续执行的包装器。快照文件包含主体身份和隐私元数据，应按敏感备份管理，禁止提交 Git 或上传公共 CI。比较器每份输入最多读取 16 MiB，拒绝重复 JSON 键、错误 schema、重复主体/审计 ID、布尔冒充整数等；超过大小应调整导出流程并另行验收，不静默截断。

| assessment / 结果 | 退出码 | 后续处理 |
| --- | --- | --- |
| `privacy_matches_reference` | 0 | 提供的两份快照通过本专项核对，继续其他恢复检查 |
| `blocked` | 2 | 存在隐私差异、未完成清理或异常数据，恢复库保持隔离 |
| `incomplete` | 3 | 输入不完整、格式错误或超过限制，重新获取有效证据 |
| 本地文件错误 | 1 | 输入不可读、输出已存在或保存失败，修复本地文件问题 |

参数错误沿用 argparse 的退出码 2。报告默认为 `target/m5e/privacy-<随机ID>.json`，使用权限 0600 的临时文件完整写入后原子发布，不覆盖已有报告。报告仅包含固定检查名、通过标志、主体总数、输入/脚本 SHA-256，不复制主体标识、审计详情、用户正文、模型输出或凭据。

被阻断后，应保持隔离，取得包含最新隐私事实的备份，或在另行审查的修复流程中补齐事实、清理数据并重新核对。当前脚本不会自动提升 epoch、重放遗忘、删除业务数据、启动角色或恢复发送。SHA-256 用于关联本次输入，不是证明输入真实性的签名。

## 验证入口

```bash
# 合成快照和实际本地 CLI，不连接数据库或真实供应商
python3 scripts/test-restore-privacy.py
python3 scripts/test-inspect-drain.py
python3 scripts/test-live-acceptance.py
python3 scripts/collect-evidence.py

# 随机隔离部署；四角色停止后导出，恢复到新库
python3 scripts/init-local.py
python3 scripts/validate-compose.py --ziwei --admission --preflight --restore-privacy
python3 scripts/check-delivery.py
```

Compose 执行以下场景：通过业务 API 创建合成记忆偏好 → 停止四角色并备份 → 恢复源库服务并请求遗忘 → 清理完成并再次停写 → 同时恢复遗忘前/后的两份备份。旧库缺少后续遗忘而被阻断，当前备份隐私匹配；全表指纹验证 SQL 和比较均无写入。另在一次性旧库中构造仅改 epoch、旧索引/召回/作业及无 owner 记录，验证无法冒充修复完成。正常的同 epoch 旧索引使用合成 SQL 夹具验证。

脚本确认这些观察时点只有 PostgreSQL 容器运行、相关库无应用连接，旧库从未配置给运行角色。被拒绝的合成旧库删除后，只将核对通过的新库接入四角色，继续验证暂停保留和恢复受理。这个隔离由本次测试编排保证；比较 CLI 本身不会阻止其他管理员或部署工具绕过流程。

原始私有快照存于本次被 Git 忽略的 `.cache/financeclaw-m5e-<随机ID>/`，CI 仅归档脱敏报告、测试结果和部署日志，不上传快照或数据库 dump。Java 运行代码、依赖及数据库迁移未改动，沿用 M5c 的 659 项 Java 回归和同一份 JAR；新 Python 契约、SQL 与容器演练单独记录，不算作新增 Java 验证。

2026-10-15 本地验证结果：**13 项隐私契约测试通过**，0 失败、0 错误、0 跳过；M5d 的 12 项预检测试及既有 8 项真实验收器离线测试也已通过。最终隔离 Compose **27 项检查通过**，包含新增 8 个隐私/隔离观察点。35 张表完整恢复并核对指纹，12 个离线请求全部完成，P95 **3.59 秒**，仍在原 15 秒阈值内；不据此推导生产容量。

归档：[本地测试与 184 个源码/配置指纹](evidence/m5e-local-results.json)、[Compose 实验](evidence/m5e-compose-results.json)、[五份脱敏隐私报告](evidence/m5e-privacy-reports.json)。三者的脚本与 JAR 摘要已核对。初次演练在只读逐表核对期间主动结束并清理，随后将相同的逐表行数/hash 公式合并为单条 SQL，加入同 epoch 旧索引场景后完整重跑；以上 27 项均来自最终这一轮。两轮随机项目的容器和卷均已清理。CI 已接入新测试和报告归档，未执行远程 CI。

## 尚未完成的迁移范围

本阶段的 `privacy_matches_reference` 仅表示所提供的记忆隐私元数据匹配；不证明参考快照最新、两库出自同一备份、所有业务数据完整或生产环境可以启动。业务 Journal、Artifact、AgentState 和渠道事实仍按原保留策略管理，本检查不宣称整个账户数据已删除。已有完整备份指纹、归属/权限、发布版本和 M5d 排空检查仍需执行。

仍待完成：真实模型/MCP/飞书验收；真实用量/成本/延迟；生产 IAM、容量/告警和 WAL/PITR；远程 CI；生产隔离与唯一写入者/渠道消费者切换；备份之后新增业务事实与遗忘事实的持久异地保存和恢复；Java → Python 反向导入。Python 历史导入仅在确定需要时开发。紫微真实模型验收未完成，普通启用门禁保留；飞书卡片、表单与可靠回执继续等待 SDK 支持。

M5e 改动暂留工作区，未提交或推送。没有操作原业务部署，也没有连接真实供应商或飞书租户。
