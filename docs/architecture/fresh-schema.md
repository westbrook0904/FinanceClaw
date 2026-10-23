# 数据库初始化与当前基线升级

FinanceClaw Java 是独立应用，支持全新数据库初始化，以及当前 Java V1 基线之后的前向升级；不导入 Python 数据或升级过去的实验结构。

主库保留 [V1 初始化 SQL](../../src/main/resources/db/migration/V1__initialize_application.sql) 的原内容和校验值。V1 创建 35 张应用表；[V2](../../src/main/resources/db/migration/V2__release_catalog.sql) 增加三张持久发布表；[V3](../../src/main/resources/db/migration/V3__conversation_release_binding.sql) 增加会话发布绑定。Flyway 按序执行并验证历史校验值，最终共 38 张表。独立遗忘账本仍使用自己的两张表和凭据，不混入主库初始化。

| 结构 | 表数 | 责任 |
| --- | ---: | --- |
| `business` | 16 | 会话、任务、命令、执行尝试、授权、交互、业务结果、归档和运行受理状态 |
| `agent_runtime` | 2 | AgentScope 原生检查点、会话工作投影和逐 Turn 完整执行轨迹 |
| `memory` | 13 | 用户画像、跨会话证据、隐私版本、提取和索引作业、恢复事实 |
| `channel` | 4 | 飞书入站去重、会话绑定与持久通知 |
| `control_plane` | 3 | 不可变发布清单、默认发布指针和不可变切换历史 |

报告的实体、动作身份与精确回执保存在同一行，并在执行围栏事务内一次插入。`id`、`title`、`action_hash`、`result` 均不可为空；不再存在历史半成品或恒定的 status 字段。`(turn_id, call_id)` 是业务唯一身份，`id` 另有唯一约束。

初始化 SQL 按依赖顺序建表。命令/尝试与交互之间存在双向外键，因此在三表建完后追加两条外键约束；这是新库建立约束所需的步骤，不是旧库升级。

Compose 的 `init-db` 服务使用结构所有者身份执行初始化或待执行迁移，并授予业务角色和受限 Memory Worker 权限。成功退出后才启动业务服务。运行进程关闭 Flyway，不能以业务数据库身份修改结构。Memory Worker 没有发布目录权限；业务角色只能追加发布清单与历史，指针允许更新。

```bash
python3 scripts/init-local.py
scripts/mvn.sh -B -ntp clean package -DskipTests
docker compose up -d --build --wait --wait-timeout 180
```

已有实验数据库不在支持范围内，代码不会清空或猜测其中的数据；开发者可为当前应用指定新的数据库或新的 Compose 项目。当前版本的备份、独立遗忘事实和恢复隔离仍然需要保留。

## 从当前 Java V1 升级

先保留数据库备份和旧启动配置，停止旧业务进程，再以新版 `init-db` 执行 V2/V3 和角色授权，最后统一启动新版业务进程。P1 不支持新旧二进制混跑；旧进程不知道会话绑定规则。Compose 中应明确重新运行初始化服务，不能仅重启旧的成功退出容器：

```bash
scripts/mvn.sh -B -ntp clean package -DskipTests
docker compose build init-db api memory-worker
docker compose stop api memory-worker
docker compose run --rm init-db
docker compose up -d --force-recreate api memory-worker
```

上述构建只生成新版 JAR 和镜像，迁移在旧业务进程停止后执行。V3 对有历史 Turn 的会话保留最近受理 Turn 的 `release_hash`（按 `created_at,id` 倒序）；没有 Turn 的旧会话在首次成功受理时绑定。新会话在创建事务内绑定。已绑定值不可原地修改。

旧库只有 hash，无法凭空还原发布正文。新进程只登记它能从启动配置生成的准确清单；若历史清单缺失，新的执行或恢复会得到 `release_unavailable`。应使用当时准确的配置重新登记并核对 hash；无法还原时保持阻断，不能替换成当前版本。已受理请求的幂等重放仍可读取原回执。

发布指针初始化后，重启或环境配置变化不会自动切换默认发布；见[发布版本管理](../operations.md#发布版本管理)。本阶段不迁移旧检查点格式，也不声称固定了硬编码提示词、Function 实现或 MCP 实时 Schema；完整依赖版本属于后续平台阶段。
