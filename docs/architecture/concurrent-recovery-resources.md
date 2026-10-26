# P3a.20：恢复、Outbox 与心跳的并发资源边界

状态：本地完整验证与 Compose 均通过。本页记录提交前证据；后续远程交付应按对应 commit 的 GitHub Actions 核验。前置 `7b46102` 已在远程分支，2026-10-26 实时复核 [GitHub Actions 40252918781](https://github.com/westbrook0904/FinanceClaw/actions/runs/40252918781) 的 verify 和 delivery 均成功。前置记录为 1,916 Java、46 脚本、44 Compose；该结果只属于前置提交。

用户要求先完成整块开发，再集中测试和修复。本阶段不为每个小改动先执行一轮 before，也不把源码审查、预期计数或合成数据写成已通过结果。

## 一次完成的三个开发块

1. **命令恢复。** 每个独立短事务查询最早一项到期命令，使用原数据库 scalar cutoff 与 `FOR UPDATE SKIP LOCKED`。每轮最多 50 个领取事务，空项即停止；不先截取 50 个无锁 ID，因此可以越过被锁的命令前缀。取得真实 Command 后仍复核 live/status，再按 Command → Turn 顺序读取回执、检查点和实时 policy。失败继续抛出：当前事务回滚，前项已提交结果保留，不自动重试模型或结果未知的外部效果。原只读 expiry mapper 仍用于健康探针和明确的只读查询实验。普通按 Command ID 和 Turn ID 的锁定读取也改为先锁定原始租约字段、再以数据库实时钟计算 live，覆盖纯锁等待期间到期的既有缺口。
2. **Outbox 定时路径。** 显式 project()/claim() 保留阻塞语义；poll() 仍绕过 idle 并执行失联恢复，但复用下述跳锁整轮路径。定时投影与领取采用有界、逐项短事务和 SKIP LOCKED，领取保持 binding → notice 锁依赖；锁内 owner、mute、revision、状态及独立 Turn 读取继续作为权威。无可锁 notice 时不回退到旧候选或发送正文，必要的本轮排除集合保持有界；不把单 tick 的空结果当作全局排空。新增 V10 前向迁移提供 pending(binding_id,created_at) 部分索引，只支持按已锁 binding 定位通知，不替代全局候选索引，也不改写已有迁移文件。sending → unknown 与 unknown 不自动重发保持。
3. **心跳资源隔离。** 在原连接总预算内预留小型心跳池，默认总数 8 分为业务 6、心跳 2；主业务 DataSource/事务身份保持。续租使用原双锁 CTE 共享常量，保留数据库实时租约、attempt/fence 和取消反馈，不允许主池耗尽时无界等待或静默回退。获取连接、语句、锁和网络超时须有界，并与租约和 tick 配置核对。资源关闭、指标及轻量 fixture 的显式装配一起完成。

不改变 sealed receipt 身份绑定、未知执行阻断、权限/隐私 epoch、外部副作用重放规则；不引入新表、通用恢复队列或迁移兼容层。

## 运行配置

主业务进程的 `spring.datasource.hikari.maximum-pool-size` 作为两个池的总预算，默认 8，分配为业务 6、心跳 2；预留后业务池必须至少剩 1 条连接。Memory Worker、初始化、发布管理和隐私恢复等受限入口不创建心跳池。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `financeclaw.worker.heartbeat-pool.reserved-connections` | 2 | 从总预算预留的连接数 |
| `financeclaw.worker.heartbeat-pool.connection-timeout-ms` | 0 | 0 按租约推导；默认租约下为 1000 ms |
| `financeclaw.worker.heartbeat-pool.statement-timeout-ms` | 0 | 0 按租约推导；默认租约下为 1000 ms |
| `financeclaw.worker.heartbeat-pool.lock-timeout-ms` | 0 | 0 按语句上限推导；默认租约下为 750 ms |

可通过 Spring 配置文件或同名 `--` 启动参数设置。配置不满足获取连接、语句上限与租约间隔约束时启动失败。驱动网络超时是秒级保护，不能证明整个调用的硬实时上限；慢锁请求过多仍可能使执行失租，Worker 必须中断，不能回退业务池续活。

指标 `financeclaw.execution.heartbeat` 只有 renewed/cancelling/fenced/error 四种结果标签，专用 Hikari pool 名为 execution-heartbeat；不将 owner、Turn、SQL 或异常正文作为标签，不新增公开指标端点。

## 实施顺序与结果

本轮先完成三个模块、普通租约锁修复及全部测试夹具，再统一格式化、检查与冻结输入，之后集中执行测试和修复。最终一次完整验证覆盖全部 146 个 Java 测试套件；没有用分次成功拼接成全量通过。

| 验证 | 实际结果 |
| --- | --- |
| 全量单元、契约与架构测试 | 86 套、1,346 项，零失败/错误/跳过 |
| 全量真实 PostgreSQL、独立 JVM 与恢复集成 | 60 套、637 项，零失败/错误/跳过 |
| Python 脚本测试 | 四套共 46 项通过 |
| 独立 Compose | 同一个 JAR，44 项部署、重启、权限、遗忘及备份恢复检查通过 |
| 输入与产物 | 518 项验证输入前后一致；当前 JAR、归档 JAR 与 Compose JAR 摘要一致 |

[完整验收记录](evidence/p3a20-implementation.json)保留三个 attempt。首次在测试代码编译阶段失败，修复资源关闭写法及重载歧义；第二次有一个测试误用了 Mockito 默认 Boolean 返回值，明确模拟 null 后修复。第三次完整 verify 退出 0，之后单独运行 Compose 并通过。各次原日志、XML、输入指纹分别保存，失败历史没有改写。

静态交叉审查另修复心跳私池 autoCommit=false 时首次回滚可能撤销初始化超时的问题：隔离 Hikari 内部查询，并用真实 PostgreSQL 在同一物理连接上验证回滚后两个超时仍有效。

## 可复用验证入口

先依次执行 scripts/test-live-acceptance.py、scripts/test-inspect-drain.py、scripts/test-restore-privacy.py 和 scripts/test-user-profile.py，再执行：

```sh
scripts/mvn.sh -B -ntp -o spotless:check verify
```

所有数量从实际 XML 解析。Maven 完成后单独执行 `python3 scripts/validate-compose.py --ziwei --admission --preflight --restore-privacy`；不能用 Java 测试通过替代 Compose 结果。

已通过的新增 PG 验收包括：50 个被锁命令前缀后第 51 项仍可推进；两个恢复调用不同项并行、同项只收尾一次；每轮 50 次上限；第二项失败保留首项提交；Outbox 锁前缀、binding/notice 新版本复核与部分提交；业务池占满时心跳使用预留池，以及租约到期、迟到续租拒绝和资源关闭。原 receipt/fence/checkpoint、权限/遗忘、取消/恢复、SDK 与部署相关回归仍由完整 verify 和后续独立 Compose 覆盖。

## 指标与边界

主要目标是减少锁竞争和资源互相阻塞，不承诺所有路径减少 SQL。命令恢复未满 50 时有终止空 probe；空轮在独立事务内查询并提交，不能隐去新增显式事务成本。SKIP LOCKED 不消除投影 upsert 遇到 suppressed notice 时的等待、Turn/owner/策略锁或池等待，也不证明跨 owner 公平、固定恢复 SLA 或全局无积压。

SQL 模板、执行计数、连接/线程分组、事务结果、池观测和安全业务事实按各夹具实际结果保存。规模复制行明确标为合成，不推断真实 provider/飞书吞吐、物理 IO 或生产容量。回执和 SDK 正文只在隔离夹具内核验，跟踪证据只写摘要和指纹。

## 运行与证据

本次本地 runner：`/tmp/financeclaw-p3a20-run-batch.py`（执行现场辅助文件，不是仓库安装前提；上方命令为可复用入口）。`--show-plan` 只读；每次执行均先冻结开发完成清单和输入指纹。每次 attempt 独占目录，保存输入快照、四组 Python 日志、Maven 原日志、原 XML、target/p3 报告和本次 JAR 指纹。摘要只记录逐 suite 计数和各文件 SHA，不重复内嵌全部 raw/caseID。runner 不提交代码、不操作 Docker、不运行 Compose、不安装依赖、不修改业务源文件。
