# 数据库初始化与当前基线升级

FinanceClaw Java 是独立应用，支持全新数据库初始化，以及当前 Java V1 基线之后的前向升级；不导入 Python 数据或升级过去的实验结构。

主库保留 [V1 初始化 SQL](../../src/main/resources/db/migration/V1__initialize_application.sql) 的原内容和校验值。V1 创建 35 张应用表；[V2](../../src/main/resources/db/migration/V2__release_catalog.sql) 增加三张持久发布表；[V3](../../src/main/resources/db/migration/V3__conversation_release_binding.sql) 增加会话发布绑定；[V4](../../src/main/resources/db/migration/V4__agent_definitions.sql) 增加 Agent 身份、不可变草稿修订及会话/Turn 的 Agent 绑定。[V5](../../src/main/resources/db/migration/V5__agent_compositions.sql) 增加可空能力选择和 v3 发布格式标记，不改写旧正文。[V6](../../src/main/resources/db/migration/V6__child_invocations.sql) 增加固定研究子调用的身份与累计预算。[V7](../../src/main/resources/db/migration/V7__execution_privacy.sql) 增加四个执行隐私函数及三个部分索引。[V8](../../src/main/resources/db/migration/V8__private_agent_delegation.sql) 扩充既有调用表的私有叶子绑定和隐私映射，P3a.3 已推送 `d433423`，[远程 CI](https://github.com/westbrook0904/FinanceClaw/actions/runs/40065079071)成功，1,702 项 Java、46 项脚本和 44 项部署检查通过。[V9](../../src/main/resources/db/migration/V9__private_agent_market_leaf.sql) 扩展固定行情私有叶子的约束和集合绑定核验。V7/V8/V9 均不增加表，Flyway 按序执行并验证历史校验值，最终仍为 41 张表。独立遗忘账本仍使用自己的两张表和凭据，不混入主库初始化。

| 结构 | 表数 | 责任 |
| --- | ---: | --- |
| `business` | 17 | 会话、任务、命令、执行尝试、授权、交互、业务结果、归档、运行受理状态及固定研究子调用 |
| `agent_runtime` | 2 | AgentScope 原生检查点、会话工作投影和逐 Turn 完整执行轨迹 |
| `memory` | 13 | 用户画像、跨会话证据、隐私版本、提取和索引作业、恢复事实 |
| `channel` | 4 | 飞书入站去重、会话绑定与持久通知 |
| `control_plane` | 5 | Agent 身份、草稿修订、不可变发布清单、默认/Agent 发布指针和切换历史 |

报告的实体、动作身份与精确回执保存在同一行，并在执行围栏事务内一次插入。`id`、`title`、`action_hash`、`result` 均不可为空；不再存在历史半成品或恒定的 status 字段。`(turn_id, call_id)` 是业务唯一身份，`id` 另有唯一约束。

`business.child_invocations` 仅保存根 Turn/工具调用 ID、研究目标或冻结的私有 Agent/发布、输入摘要、固定权限、pending/completed 及累计计数；研究为 4/10、空工具叶子最多 4/0、固定行情叶子最多 4/1。owner、会话、发布、epoch 和 grant revision 从根受理记录 JOIN，结果复用 `capability_steps`；没有子队列、子租约或输入/结果副本。V6 给已存在的业务角色显式授予新表 CRUD 权限，Memory Worker 不获得访问权限。详见[固定研究子调用](child-invocations.md)。

初始化 SQL 按依赖顺序建表。命令/尝试与交互之间存在双向外键，因此在三表建完后追加两条外键约束；这是新库建立约束所需的步骤，不是旧库升级。

Compose 的 `init-db` 服务使用结构所有者身份执行初始化或待执行迁移，并授予业务角色和受限 Memory Worker 权限。成功退出后才启动业务服务。运行进程关闭 Flyway，不能以业务数据库身份修改结构。Memory Worker 没有发布目录权限；业务角色只能追加发布清单与历史，指针和 Agent 当前草稿 revision 允许更新，草稿正文只允许追加。

```bash
python3 scripts/init-local.py
scripts/mvn.sh -B -ntp clean package -DskipTests
docker compose up -d --build --wait --wait-timeout 180
```

已有实验数据库不在支持范围内，代码不会清空或猜测其中的数据；开发者可为当前应用指定新的数据库或新的 Compose 项目。当前版本的备份、独立遗忘事实和恢复隔离仍然需要保留。

## 从当前 Java V1–V8 升级

先保留数据库备份和旧启动配置，停止旧业务进程，再以新版 `init-db` 执行尚未应用的 V2–V9 和角色授权，最后统一启动新版业务进程。本次升级不支持新旧二进制混跑；旧进程不能提供新版本的私有目标绑定及执行隐私映射。Compose 中应明确重新运行初始化服务，不能仅重启旧的成功退出容器：

```bash
scripts/mvn.sh -B -ntp clean package -DskipTests
docker compose build init-db api memory-worker
docker compose stop api memory-worker
docker compose run --rm init-db
docker compose up -d --force-recreate api memory-worker
```

上述构建只生成新版 JAR 和镜像，迁移在旧业务进程停止后执行。V3 对有历史 Turn 的会话保留最近受理 Turn 的 `release_hash`（按 `created_at,id` 倒序）；没有 Turn 的旧会话在首次成功受理时绑定。新会话在创建事务内绑定。已绑定值不可原地修改。V4 将历史会话与 Turn 的 Agent 身份回填为 `finance_agent`，保留旧清单正文、hash 与检查点命名空间；新建自定义会话同时绑定 Agent ID 和发布 hash，Turn 目标也不可修改。

旧库只有 hash，无法凭空还原发布正文。新进程只登记它能从启动配置生成的准确清单；若历史清单缺失，新的执行或恢复会得到 `release_unavailable`。应使用当时准确的配置重新登记并核对 hash；无法还原时保持阻断，不能替换成当前版本。已受理请求的幂等重放仍可读取原回执。

发布指针初始化后，重启或环境配置变化不会自动切换默认发布；见[发布版本管理](../operations.md#发布版本管理)。P2a 用 `agent-release/2` 清单固定自定义 Agent 的提示词、owner 和基础发布，旧格式继续独立解码且不能作为私有 Agent 的别名。本阶段不迁移旧检查点格式；P2b 新增 `agent-release/3`，冻结受控能力、SDK 有效 MCP 契约和生产运行工件摘要，旧格式不会获得补造快照，见 [能力组装与升级限制](agent-composition.md#冻结的范围与升级行为)。

V6 不回填旧研究步骤的调用绑定或预算。已有 `child:<call_id>` 步骤但缺少调用绑定时拒绝复用，不把未知消耗当成零。父请求的原回执、取消/失租围栏和未知执行阻断规则保持权威；本阶段不提供子检查点向新 attempt 的自动复制。

V7 的函数在后续遗忘或隔离恢复重放时清理旧执行副本；应用结构变更本身不等于已清理历史正文。SDK 状态先保留递增版本墓碑，再删除旧步骤、子调用及隐私绑定工具文件，不移除普通业务 Turn、Journal、报告或 Artifact。Snapshot v3 必须使用 V7 的全局残留函数，v2 不再作为当前恢复证明；旧快照不能补零后继续使用，应在已升级的隔离库重新导出。具体范围、未知状态阻断与验收状态见[执行数据隐私清理](execution-privacy.md)。

V8 增加 `target_agent_id`、`target_release_hash` 和私有绑定不可变约束；只允许同 owner 显式空工具叶子，原市场研究字段及 scope 不回填或改写。集合恢复函数改用保存的目标和历史 attempt 派生私有 scope，不读取 current。运行仍严格校验生产工件摘要，升级后二进制不匹配的旧组合发布保持阻断；应先用新运行工件发布叶子，再重新发布父 Agent 并创建新会话，不能重写旧会话或旧目标 hash。配置和本地验收范围见[私有叶子 Agent 委派](private-agent-delegation.md)。

V9 不改变旧空工具契约、调用身份或 scope 派生式；新行情档位最多一次读取，目标计划按冻结元数据严格匹配。升级后仍先发布新工件的目标与父 Agent，再创建新会话。配置与本地验证范围见[固定行情叶子](private-agent-market-leaf.md)。
