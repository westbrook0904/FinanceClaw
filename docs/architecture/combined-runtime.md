# 同进程运行与事件推送

当前 Java 部署把 HTTP API、AgentScope 执行、SSE 和飞书投递放在一个 Spring Boot 进程。它们保留各自的业务职责，共用一个业务连接池，无需沿用 Python 的多个业务进程。工程仍为单个 Maven 模块。

| Compose 服务 | 用途 |
| --- | --- |
| `api`，启动参数 `standalone` | HTTP、AgentScope 执行与实时事件、恢复和飞书投递 |
| `memory-worker` | 记忆提取、索引与隐私清理；使用独立受限数据库身份 |
| `migrate` | 一次性数据库迁移，成功退出后才启动运行进程 |
| `postgres` | 持久任务、权限、执行状态、业务结果及记忆事实 |

`api` 保留原 Compose 服务名，既有端口、secret 挂载、遗忘账本 overlay 和运维入口可继续使用。`Dockerfile` 与容器入口默认运行 `standalone`；容器 profile 关闭 Flyway。裸 `scripts/mvn.sh spring-boot:run` 也会组合运行上述业务职责，并保留开发库自动迁移。

## SSE 与多用户

实时 SSE 从同进程的 AgentScope 事件及任务状态通知推送，连接建立及重连按事件游标读取持久历史；空闲连接不循环查询数据库。业务完成仍由持久任务状态与回执决定，连接结束不等于任务完成。

当前支持一个业务 JVM 内的多用户、多个会话并行执行。生产身份认证仍需单独接入，默认配置的 Bearer token 只映射一个主体。实时推送通道尚未接入跨实例消息代理，因此不能通过新增独立 Worker 或增加业务容器副本来扩容 SSE；另一个进程执行的实时事件不会自动到达当前连接。旧 `api` / `worker` / `integrations` profile 仅保留兼容和隔离测试，拆分 API 的实时 SSE 返回明确的不可用响应。

执行并行度、数据库连接数和请求延迟需要在实际模型、工具和负载下测量。合并进程减少重复连接池和跨进程事件读取，不意味着数据库成本已消失：任务受理、审批、租约、状态保存、结果、隐私与持久事件仍需事务和数据库校验。

## 启动和升级

新环境使用 README 的容器启动命令。已有拆分部署应先暂停新任务，核对活动任务和不确定副作用，等待可安全停止的边界；使用旧版本 Compose 文件停止旧业务进程：

```bash
# 在更新 Compose 文件之前执行；保留数据库和数据卷
docker compose stop api worker integrations

# 更新到当前版本后构建并启动，清除同项目已经停止的旧角色容器
scripts/mvn.sh -B -ntp package -DskipTests
docker compose build api
docker compose up -d --wait --wait-timeout 180 --remove-orphans
```

该命令不会停止其他项目或手动启动的 JVM，操作前应核对所有旧执行者。不要让旧独立 Worker 和新的 `standalone` 同时消费同一库。旧任务是否可以恢复仍取决于固定发布版本、SDK 状态和既有执行回执，不能用进程合并绕过审批或重放未知写操作。

当前部署的整体停写命令是：

```bash
docker compose stop api memory-worker
```

隐私恢复继续使用独立的 `privacy-recovery` 入口，保持业务 API、模型执行和渠道投递不启动。恢复旧备份前按 [M5e 核对](../migration/m5e-restore-privacy.md)和 [M5f 遗忘重放](../migration/m5f-forget-ledger.md)完成隔离与全主体隐私核对；不能用 `standalone` 连接未经核对的恢复库。

## 验证入口

`python3 scripts/validate-compose.py --ziwei --admission --preflight --restore-privacy` 验证当前默认拓扑，包含同进程执行与飞书投递、停用执行后的排队及重启恢复、整个业务 JVM 的 SIGKILL 后审批恢复、数据库中断、受限记忆、隔离备份和旧隐私快照拒绝。脚本始终使用随机项目、新数据卷、离线模型和合成数据。

旧 M5 / R6 文档中的“四角色健康”和独立 Worker 故障数据是各自批次的历史结果，不证明当前拓扑已经通过容器运行验收；本次实际检查结果以本次执行日志和交付说明为准。

2026-10-22 首批检查点（`ae16c36`）的默认拓扑的上述容器验收已通过 28 项；最终完整 Maven 验证通过 950 项单元/契约/架构测试和 275 项集成测试。见[本次验证结果](database-access-review.md#首批检查点验证结果)及[隔离容器证据](evidence/combined-runtime-compose.json)。模型与外部工具均使用离线或本地模拟环境。

后台任务队列使用提交后进程内通知，并以最长 5 秒的空闲退避扫描兜底；过期执行每 2 秒独立恢复。心跳每 2 秒单 SQL 续租，使用专用调度线程，仍共享业务数据库连接池。V10 已退役没有业务读取方的本地 Outbox 投影链路及停用的 Skill/M0 表，升级前按[表退役操作](../../ops/retire-legacy-tables.md)停旧版本并备份。具体次数、并发边界及本轮验证见[数据库访问说明](database-access-review.md)。
