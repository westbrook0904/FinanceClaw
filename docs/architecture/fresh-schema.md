# 新应用数据库初始化

FinanceClaw Java 是独立应用，只为全新数据库创建当前结构，不导入 Python 数据或升级过去的实验结构。

主库使用一份 [V1 初始化 SQL](../../src/main/resources/db/migration/V1__initialize_application.sql)，直接创建 34 张应用表。Flyway 只负责记录初始化与校验文件完整性；`db/migration` 是 Flyway 的默认资源路径，不表示项目提供历史版本迁移。独立遗忘账本使用自己的两张表和凭据，不混入主库初始化。

| 结构 | 表数 | 责任 |
| --- | ---: | --- |
| `business` | 16 | 会话、任务、命令、执行尝试、授权、交互、业务结果、归档和运行受理状态 |
| `agent_runtime` | 1 | AgentScope 原生状态的版本化保存与墓碑 |
| `memory` | 13 | 用户画像、跨会话证据、隐私版本、提取和索引作业、恢复事实 |
| `channel` | 4 | 飞书入站去重、会话绑定与持久通知 |

报告的实体、动作身份与精确回执保存在同一行，并在执行围栏事务内一次插入。`id`、`title`、`action_hash`、`result` 均不可为空；不再存在历史半成品或恒定的 status 字段。`(turn_id, call_id)` 是业务唯一身份，`id` 另有唯一约束。

初始化 SQL 按依赖顺序建表。命令/尝试与交互之间存在双向外键，因此在三表建完后追加两条外键约束；这是新库建立约束所需的步骤，不是旧库升级。

Compose 的 `init-db` 服务使用结构所有者身份执行一次初始化，并授予业务角色和受限 Memory Worker 权限。成功退出后才启动业务服务。运行进程关闭 Flyway，不能以业务数据库身份修改结构。

```bash
python3 scripts/init-local.py
scripts/mvn.sh -B -ntp clean package -DskipTests
docker compose up -d --build --wait --wait-timeout 180
```

启动目标必须是本应用的新库。已有实验数据库不在支持范围内，代码不会清空、猜测或自动修补其中的数据；开发者可为当前应用指定新的数据库或新的 Compose 项目。不要将生产备份恢复机制与历史应用迁移混为一谈：当前版本的备份、独立遗忘事实和恢复隔离仍然需要保留。
