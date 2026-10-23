# M5f：遗忘事实独立保存与恢复重放

> **历史记录**：本文保留当时的设计、命令与验证结果，不代表当前独立应用的部署结构或最新测试结论。当前操作见[运行说明](../operations.md)，新库结构见[初始化说明](../architecture/fresh-schema.md)；历史类名、版本号和外部导出命令仅用于理解证据。

日期：2026-10-21。本文件是当前使用说明；[M5e](m5e-restore-privacy.md) 保留两份隐私快照的严格比较协议及当时的验证记录。

启用独立遗忘账本后，`POST /v1/memory/forget` 只有在遗忘事实已提交到独立 PostgreSQL、主库也已应用清理和幂等回执后才返回成功。恢复旧主库时，可以从仍然完整、最新的独立账本补回备份之后的遗忘。**恢复库必须始终隔离，直到重放成功并完成业务恢复核对。**

该能力需要先初始化并启用。默认关闭；未启用时仍沿用原有主库事务，不提供独立保存保证。已经激活的主库不能通过关闭配置退回旧路径，遗忘请求会返回 `forget_ledger_required`。

## 当前架构

| 位置 | 保存内容 | 作用 |
| --- | --- | --- |
| 主库 `memory.privacy_ledger_binding` | 数据库 lineage、是否已激活 | 随主库备份保留恢复身份，防止接错遗忘历史 |
| 主库 `memory.forget_requests` | 待完成请求的稳定事件 ID、owner、原 epoch、幂等键及请求 hash | 在访问外部账本前保存请求和读取屏障，支持故障后继续同一请求 |
| 独立库 `privacy_ledger.forget_facts` | 同一份遗忘事实，另有记录时间 | 在主库回退或丢失时保留遗忘依据；运行账号只能查询、追加事实 |
| 主库 `memory.forget_applied`、`memory.operations`、`memory.audit` | 已应用事件、原幂等回执、遗忘及清理审计 | 重复重放不增加 epoch，原请求可以继续用原 key 重试 |

遗忘事实不保存记忆正文、模型输出或数据库凭据，但 owner 和幂等键仍是私有元数据。账本、备份和管理查询结果均须受限保存，不能上传公共 CI 或提交 Git。

两个数据库分别使用 Spring 事务。主库适配器使用参与原事务的 `JdbcTemplate`；外账本使用自己的连接池、`JdbcTransactionManager` 和 `TransactionTemplate`，写事务显式设置 `synchronous_commit=on`。外账本连接池不注册为业务 DataSource，业务 MyBatis 和 Flyway 仍使用原主库。

外账本必须有独立的存储、运行权限和备份生命周期，不能在主库内换一个 schema 代替。适配器发现目标库包含主库的 `memory.privacy_ledger_binding` 时拒绝操作。在同一主机上开另一个数据库或进程只提供逻辑隔离，不能据此声称异地容灾；生产还需保证账本本身的耐久性、最新性和可恢复性。

## 三阶段请求与故障处理

| 阶段 | 提交内容 | 中断后的处理 |
| --- | --- | --- |
| 1. 主库准备 | 锁定绑定和 owner，核对 epoch/幂等键，保存 pending 请求并设置 `purge_pending=true` | 请求与屏障保留；此时不代表外账本已经耐久保存 |
| 2. 独立保存 | 以稳定事件 ID 和原幂等键向外账本追加事实 | 失败或提交结果未知时保留主库屏障，同一请求重试；不得换 key 绕过 |
| 3. 主库应用 | 重查 pending 身份，清理旧 epoch 内容/引用/租约，提升 epoch，保存审计和幂等回执，删除 pending | 事务失败全部回滚到准备状态；再次重试复用已存在的独立事实 |

准备后的屏障会阻止旧记忆继续被召回、搜索和使用，并阻止该主体受理新任务或确认候选记忆。Memory Worker 不会清除仍有 pending 请求的屏障；已经发出的模型调用仍按现有 epoch/租约检查阻止过期结果写回。其他主体继续按原规则工作。

同 key、同参数重试收敛为同一事件和一次 epoch 增长；同 key 换参数返回幂等冲突。已有 pending 时，新 key 不能覆盖它。外账本提交失败或结果未知时，请求返回失败，不把准备阶段表述为遗忘成功；如果此时主库又丢失，尚未写入外账本的请求不具有独立恢复保证。

阶段 3 完成后，正常运行仍由 Memory Worker 写入 `purge_complete` 并解除清理标志。离线重放入口在完成全部重放和残留核对后完成该步骤。不要手动删除 pending 或修改 `purge_pending` 来解除阻塞。

## 首次启用

先停止业务进程、Memory Worker，以及其他可能写入主库或发送渠道消息的进程。当前默认 `api` 合并 HTTP、执行和投递；旧部署中的独立 Worker / Integrations 也必须停止。需要覆盖所有副本；单个 owner 的排空报告不能证明全局停写。隔离期间只允许迁移和恢复管理连接。

已有 Compose 部署可先执行：

```bash
docker compose stop api memory-worker
```

这条命令只停止当前 Compose 项目中的对应服务，不会停止其他部署或手动启动的 JVM。隔离期间不要运行会启动全部角色的 `docker compose up`。

### 1. 当时的主库 V7 初始化（历史步骤）

当时 V7 创建并保存 lineage，初始 `active=false`。当前独立应用已将该结构并入[新库初始化 SQL](../../src/main/resources/db/migration/V1__initialize_application.sql)，只支持新库初始化。下方类名和容器命令已更新到当前入口用于定位，旧应用数据库不能直接套用；现行初始化与恢复步骤以[运行说明](../operations.md)为准。

```bash
java -Dloader.main=com.financeclawsetup.DatabaseInitializationApplication \
  -cp target/financeclaw-java-0.1.0-SNAPSHOT.jar \
  org.springframework.boot.loader.launch.PropertiesLauncher
```

默认 Compose 的 PostgreSQL 没有映射宿主机端口。Compose 部署在数据库保持运行、所有运行角色保持停止时，使用下面的容器内迁移流程；先完成当前代码的 JAR 构建，再重建共享镜像，避免继续使用不包含 M5f 的旧镜像。当前 `init-db` 服务会同时执行 V7 及仓库中的角色授权配置。

```bash
docker compose build api
docker compose run --rm --no-deps init-db
```

迁移完成后重新配置运行权限，让所需角色可访问新增的 `memory` 表。主库迁移不会初始化外账本；不要把外账本 DDL 放进主库 Flyway 的迁移目录。

### 2. 初始化独立 PostgreSQL

由管理员创建独立数据库和运行账号，使用 [外账本 DDL](../../src/main/resources/privacy-ledger/V1__forget_ledger.sql) 初始化一次。以下 `PGSERVICE` 必须预先配置到外账本管理员连接；密码由受限 passfile 或密钥管理提供：

```bash
PGSERVICE=financeclaw-ledger-admin psql -X -v ON_ERROR_STOP=1 \
  -f src/main/resources/privacy-ledger/V1__forget_ledger.sql
```

DDL 文件末尾列出运行账号的最小授权：schema `USAGE`，`lineages` 的 `SELECT/INSERT/UPDATE`，`forget_facts` 的 `SELECT/INSERT`。`lineages` 的 `UPDATE` 权限用于行锁；应用不修改已经保存的遗忘事实。运行账号不应拥有表或 schema，不授予事实表 `UPDATE/DELETE/TRUNCATE`，也不授予数据库或 schema 的创建权限。初始化 DDL 不是可反复执行的清空脚本。

### 3. 配置外账本连接

| 环境变量 | Spring 属性 | 含义 |
| --- | --- | --- |
| `FINANCECLAW_FORGET_LEDGER_ENABLED` | `financeclaw.memory.forget-ledger.enabled` | 显式设置 `true`；默认 `false` |
| `FINANCECLAW_FORGET_LEDGER_URL` | `financeclaw.memory.forget-ledger.url` | 独立库的 `jdbc:postgresql:` URL |
| `FINANCECLAW_FORGET_LEDGER_USER` | `financeclaw.memory.forget-ledger.username` | 外账本受限运行账号 |
| `FINANCECLAW_FORGET_LEDGER_PASSWORD` | `financeclaw.memory.forget-ledger.password` | 由密钥管理注入的密码 |

主库仍使用 `FINANCECLAW_DATABASE_URL/USER/PASSWORD`。恢复入口与应用使用相同的配置来源；密码可以通过 Spring configtree 的 `financeclaw.memory.forget-ledger.password` 属性文件提供。不要把密码作为 JVM 参数或写入文档、脚本和 shell 历史。

处理遗忘请求的 API/standalone 进程和离线恢复入口必须持续获得以上外账本配置；其他角色无需持有外账本凭据。正常使用记忆 API 还需要 `FINANCECLAW_MEMORY_ENABLED=true`，外账本开关不会自动开启记忆功能。Compose 或其他部署平台需要显式透传配置并挂载密钥；仅修改宿主机环境变量不会自动更新已经运行的容器。

仓库提供可选 [Compose 覆盖文件](../../compose.forget-ledger.yml)：将外账本密码由密钥管理保存到 `.secrets/forget-ledger-password`，设置 URL 与 USER 后，使用两个 Compose 文件启动 API。本地 Compose 的文件密钥保留宿主机权限，需要允许容器 UID 10001 读取；沿用仓库策略，将 `.secrets` 目录设为 `0700`，此密钥文件设为 `0444`，或使用部署平台的等价受限挂载。不要直接把文件设为仅宿主机用户可读的 `0600` 后挂载给容器。该覆盖文件只连接已有独立账本，不会创建或清空账本，也不把主库密码用作外账本密码。

```bash
# 保持运行角色停止，进行一次性 bootstrap，不启动依赖服务。
# 使用步骤 1 构建的 M5f 镜像；主库 V7、外账本和凭据已准备好，数据库保持可连接。
docker compose -f compose.yml -f compose.forget-ledger.yml run --rm --no-deps api \
  privacy-recovery --financeclaw.privacy-recovery.isolated=true \
  --financeclaw.privacy-recovery.action=bootstrap
```

完成下一节要求的基线备份后，正常启动也持续带上 `-f compose.yml -f compose.forget-ledger.yml`。默认 Compose 不启用外账本。

### 4. 在停写状态下执行 bootstrap

bootstrap 要求历史遗忘 epoch 连贯、记忆残留已清理、没有 pending 请求，并且存在最新 epoch 的清理完成审计。若源库的历史遗忘尚在清理中，先通过原 Memory Worker 流程完成清理，再重新停写；不要手改清理标志。历史遗忘的原幂等回执必须可恢复；不能用补造 key 或直接提高 epoch 修复不完整历史。

```bash
java -Dloader.main=com.financeclawprivacy.PrivacyRecoveryApplication \
  -cp target/financeclaw-java-0.1.0-SNAPSHOT.jar \
  org.springframework.boot.loader.launch.PropertiesLauncher \
  --financeclaw.privacy-recovery.isolated=true \
  --financeclaw.privacy-recovery.action=bootstrap
```

成功时进程退出码为 `0`，JSON 的 `result` 为 `privacy_ledger_initialized`，并包含事实数、主体数和本次历史指纹。命令会将当前完整遗忘历史写入独立账本，核对结果后激活主库绑定。空历史也需要 bootstrap，以便绑定和初始化 lineage。

如果账本已经成功写入而主库激活失败，保持停写，修复故障后重复同一 bootstrap。若外账本比当前主库历史更靠前，不能通过 bootstrap 覆盖它，应保持隔离并核对恢复来源。

`--financeclaw.privacy-recovery.isolated=true` 是操作者对隔离条件的显式声明，程序不会代替部署平台停止其他进程。该入口强制关闭 Web、调度角色和 Flyway，不会启动模型或渠道出站。

### 5. 制作启用后的基线备份

bootstrap 成功后继续保持停写，立即制作并验证一份包含 **V7 lineage 和 `active=true`** 的完整主库基线备份。后续用于本功能的旧备份都必须源于该基线，并关联同一份仍然最新的独立账本。

```bash
umask 077
mkdir -p backups
PGSERVICE=financeclaw-primary-admin pg_dump -Fc \
  -f backups/financeclaw-ledger-baseline.dump
shasum -a 256 backups/financeclaw-ledger-baseline.dump \
  > backups/financeclaw-ledger-baseline.dump.sha256
```

上例文件名是一次性示例，后续备份应采用不同名称，保留原基线和校验记录。备份、校验和及外账本的运维记录按私有备份管理。完成基线和配置核对后，再按既有部署流程启动原环境。

**V7 之前的备份、未完成 bootstrap 的备份不在自动重放支持范围内。** 不要在旧备份上临时执行 V7、复制或手填 lineage，再将其当作已启用基线；必须先另行核对其来源和完整历史。

## 恢复旧备份并重放

1. 停止所有旧部署的写入与出站，将备份恢复到隔离的新数据库，保留 owner 和权限归属。不要覆盖当前库，也不要恢复或回退独立账本到同一旧时间点。
2. 核对主库已包含 V7、绑定 `active=true`，且 lineage 对应本次部署。确认外账本包含备份之后全部已成功遗忘的最新记录；同一份旧账本的重复读取不能证明它最新。
3. 将恢复命令的主库配置指向恢复库，外账本配置仍指向最新独立账本。所有运行角色继续停止，运行以下命令：

```bash
java -Dloader.main=com.financeclawprivacy.PrivacyRecoveryApplication \
  -cp target/financeclaw-java-0.1.0-SNAPSHOT.jar \
  org.springframework.boot.loader.launch.PropertiesLauncher \
  --financeclaw.privacy-recovery.isolated=true \
  --financeclaw.privacy-recovery.action=replay
```

重放先用原身份补交恢复库中仍然 pending 的已授权请求，再读取账本一致性快照，验证完整事件链、原回执和本地历史。主库事务按事件顺序清理 `epoch < 目标 epoch` 的来源正文、事实正文/证据、索引、召回和作业租约，补齐本地审计与回执，再核对全部残留。当前 epoch 的新记忆不会因重复重放被清除。

账本丢失中间 epoch、事件或幂等键冲突、恢复库历史领先账本、未来 epoch 数据、未清理残留都会阻断。完成后会再次读取账本；如果期间发生变化，仍返回阻断，保持隔离后重新核对。重复运行不会额外提升 epoch，也不会重复产生已存在的遗忘回执。

| 结果 | 退出码 | 处理 |
| --- | --- | --- |
| `privacy_ledger_initialized` | 0 | bootstrap 完成，仍需制作启用后的基线备份 |
| `privacy_replayed` | 0 | 本次账本与恢复库通过重放和残留核对，继续其他业务恢复检查 |
| `blocked` / `isolation_required` | 2 | 未提供隔离声明，命令未启动恢复流程 |
| `blocked` / `privacy_recovery_failed` | 2 | 配置、连接、历史或应用失败，恢复库保持隔离 |

CLI 只接受文中的隔离声明和 action 两个参数；连接参数通过环境或 configtree 注入，重复/额外参数返回 `invalid_recovery_arguments`。恢复 JVM 在框架初始化、执行和关闭期间屏蔽标准诊断输出，并使用专用日志配置；包括 JDBC TRACE/Hikari DEBUG 在内的正常日志配置不会把私有元数据写入 CLI 输出。命令仅输出固定错误码或 `result/facts/owners/fingerprint` 报告，不输出异常详情、owner、幂等键、正文或凭据。指纹用于关联本次事实集合，不是来源真实性的签名。需要归档 stdout 时，使用权限受限的新文件并同时记录进程退出码；不能将“已有报告文件”当作命令成功。

## 放行边界与排障

`privacy_replayed` 说明当前独立账本对应的记忆遗忘已在恢复库应用，**不等于全业务恢复放行**。它不证明外账本由其他系统提供的最新性，也不恢复备份之后新增的任务、Journal、Artifact、审批、执行副作用或渠道事实。仍须完成全库备份指纹、权限/归属、发布版本、活动任务、不确定外部调用、通知及唯一写入者/消费者核对，才能决定何时启动各角色。

M5e 的 `privacy_matches_reference` 要求两份快照中的原 audit ID 和时间逐项一致。重放会在恢复库生成缺失的本地清理审计，因此 `privacy_replayed` 是另一个验证协议，不能冒充 M5e 的源库审计完全匹配，也不改变旧比较器的严格语义。

| 现象 | 核对方向 |
| --- | --- |
| `forget_ledger_uninitialized` | 先停写并完成 bootstrap，不直接修改 active |
| `forget_ledger_required` | 数据库已激活，恢复正确的外账本配置；不能关闭配置绕过 |
| `forget_ledger_unavailable` | 核对独立库连接、权限及可用性，保留 pending，用原 key/epoch 重试 |
| `forget_pending` | 同一主体已有待完成请求，继续原请求，不提交新 key 覆盖 |
| 恢复入口返回固定 `privacy_recovery_failed` | 在受限管理环境核对 V7/绑定、账本权限、连续历史、回执和残留；保持隔离，不把数据库异常内容复制到公共日志 |

本版单个 lineage 最多处理 100,000 条遗忘事实；超过限制会阻断，不能截断历史后宣称成功。扩容和分页方案需要单独设计与验证。生产账本备份、异地保存、IAM、告警及主库 WAL/PITR 仍需部署验收；本功能不将其自动视为完成。

## 实现入口与验证记录

- [遗忘事实值对象](../../src/main/java/com/financeclaw/domain/memory/ForgetFact.java)
- [三阶段遗忘用例](../../src/main/java/com/financeclaw/app/memory/MemoryForgetService.java)
- [bootstrap/replay 用例](../../src/main/java/com/financeclaw/app/memory/PrivacyRecoveryService.java)
- [主库恢复适配器](../../src/main/java/com/financeclaw/infrastructure/memory/JdbcForgetRecoveryRepository.java)
- [外账本适配器](../../src/main/java/com/financeclaw/infrastructure/memory/PostgresForgetLedger.java)
- [离线 JVM 入口](../../src/main/java/com/financeclawprivacy/PrivacyRecoveryApplication.java)

2026-10-21 的最终完整验证命令：

```bash
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/test-live-acceptance.py
python3 scripts/test-inspect-drain.py
python3 scripts/test-restore-privacy.py
python3 scripts/collect-evidence.py
```

同一份最终 Java 构建 **1,186 项通过**（912 单元/契约/架构、274 PostgreSQL/独立 JVM 集成），0 失败、0 错误、0 跳过，Spotless 通过。另有 8 项已有验收器、12 项排空预检和 13 项 M5e 隐私比较契约测试通过。机器证据及源文件/JAR 指纹见 [M5f 验证结果](evidence/m5f-results.json)。

其中新增 59 项集成测试：

| 测试 | 数量 | 主要覆盖 |
| --- | ---: | --- |
| `ForgetLedgerIT` | 14 | 独立 PostgreSQL、追加幂等、事件链、冲突、隔离快照及最小权限 |
| `ForgetRecoveryRepositoryIT` | 26 | V7、pending 屏障、事务回滚、按 epoch 清理、审计顺序、残留检查和 Memory Worker 权限 |
| `ForgetRecoveryIT` | 9 | 两个数据库、实际 `pg_dump` 后源库清除/还原、备份后两轮遗忘重放、未知提交结果、并发同 key、关闭配置防绕过及 bootstrap 重试 |
| `PrivacyRecoveryCliIT` | 10 | 实际打包 JAR 的独立 JVM、bootstrap/replay、pending 恢复、参数隔离、禁止自动迁移及 JDBC TRACE/Hikari DEBUG 下的输出脱敏 |

旧备份测试恢复的是启用后的旧主库，独立账本保留备份之后的事实；重复重放不增加 epoch，也保留当前 epoch 的新记忆。这是本地真实 PostgreSQL 故障实验，不是生产存储故障域或最新权威来源的运维验收。

同一 JAR 的四角色部署回归也已通过：

```bash
python3 scripts/validate-compose.py --ziwei --admission --preflight --restore-privacy
```

**27 项 Compose 检查通过**，包括 V7 空库迁移、角色权限、进程强杀、数据库中断、38 张表的完整备份还原、旧备份隐私阻断和恢复后受理；见 [部署证据](evidence/m5f-compose-results.json)。此轮使用默认关闭外账本的 Compose 配置，验证已有部署流程与 V7 的兼容性。可选外账本覆盖文件已通过 `docker compose config --quiet` 语法检查；启用外账本后的实际双库协议与恢复 JVM 由上表的 PostgreSQL 集成测试覆盖，未把它表述为完整生产部署验收。

前一轮回归发现恢复 CLI 的启动 DEBUG 日志泄露风险，以及角色装配仍断言 6 个迁移版本；两处均已修复，并完成一次上述完整重跑。最终结果来自这次单独完整通过的构建，不拼接早期失败运行的结果。以上为本地验证记录，远程 CI 结果另行记录；真实供应商、生产 IAM、异地容灾及线上切换不在本次通过范围内。
