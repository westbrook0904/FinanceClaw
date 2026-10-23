# FinanceClaw Java

基于 Java 21、Spring Boot 和 AgentScope Java 的独立 Agent 应用。它提供持久任务、受治理工具执行、人工确认、结果归档、手工用户画像和跨会话证据记忆。默认离线运行，不依赖 Python 应用或其历史数据。

HTTP API、AgentScope 执行、SSE、恢复与飞书投递运行在一个 JVM。受限 Memory Worker、新库初始化和离线隐私恢复各有独立入口。工程使用单个 Maven 模块，当前部署保持一个业务副本。

## 能力与边界

- 任务受理具有幂等键、主体归属和持久命令；执行使用租约 token、检查点、取消与恢复围栏。
- 工具调用保留授权版本、动作绑定和人工确认；报告与精确回执一次事务写入，归档结果按主体和权限读取。
- SSE 使用同进程事件推送，重连按持久游标回放，空闲连接不轮询数据库。
- 用户画像通过 `profile.md` 手工维护，带 ETag 和隐私版本；证据记忆独立保存来源、确认状态和向量投影。
- 酒店/Taibu MCP、调酒 Skill、市场研究子 Agent、固定投资组合工作流和紫微工具通过原生 AgentScope 能力接入。真实供应商、飞书租户与生产容量需要另行验收；紫微继续保留开发启用门禁。
- 备份恢复前核对独立遗忘事实，阻止旧备份重新引入已遗忘资料。该机制服务于当前应用恢复，不是历史系统迁移。

数据库从[单一 V1](src/main/resources/db/migration/V1__initialize_application.sql)直接创建 34 张应用表。不提供历史库导入、版本升级回填或旧接口空值兼容。Flyway 仅记录当前初始化；详见[新库初始化](docs/architecture/fresh-schema.md)。

## 本地启动

需要 Docker、Python 3.12+（工具链安装和验收脚本）及 curl。运行镜像和业务逻辑不依赖 Python；构建脚本使用锁定的 Java 21 和 Maven 版本。

```bash
python3 scripts/bootstrap-jdk.py
python3 scripts/init-local.py
scripts/mvn.sh -B -ntp clean package -DskipTests
docker compose up -d --build --wait --wait-timeout 180
curl --fail http://localhost:8080/actuator/health/readiness
```

Compose 创建新库，`init-db` 完成结构和权限初始化后，启动统一业务服务 `api` 与受限 `memory-worker`。API 仅绑定本机 `8080`，配置在 `.env.example`，凭据由 `.secrets/` 经 configtree 挂载，不提交到仓库。

```bash
export FINANCECLAW_API_TOKEN="$(cat .secrets/api-token)"
curl --fail -H "Authorization: Bearer $FINANCECLAW_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{}' \
  http://localhost:8080/v1/conversations
```

创建会话后，向 `/v1/conversations/{conversation}/turns` 提交 `{"message":"你好"}`，同时提供 `Idempotency-Key`。查询 `/turns/{turn}` 获取持久状态，`/turns/{turn}/events` 获取 SSE。默认离线回复为 `已收到：<消息>`。HTTP 请求遵循[统一严格类型校验](docs/architecture/http-request-validation.md)。

默认认证 token 映射一个服务端主体。多用户生产环境应接入可信认证源，不能在请求正文中自行传入 tenant/subject。不要用多个独立业务副本扩展当前 SSE，跨实例事件转发尚未实现。

## 验证

```bash
python3 scripts/check-delivery.py
python3 scripts/test-live-acceptance.py
python3 scripts/test-inspect-drain.py
python3 scripts/test-restore-privacy.py
python3 scripts/test-user-profile.py
scripts/mvn.sh -B -ntp clean verify
python3 scripts/collect-evidence.py
python3 scripts/validate-compose.py --ziwei --admission --preflight --restore-privacy
```

数据库测试使用 Testcontainers 和独立数据库；模型、MCP、embedding 与飞书使用本地模拟服务。Compose 使用随机项目和新数据卷，覆盖进程故障、受理排空、画像隔离、备份恢复和隐私拦截。真实模型与第三方验收入口为 `scripts/validate-live.py`，它们不属于默认离线测试。

验证汇总写入 `target/verification/results.json`，Compose 另有部署证据。测试源码、报告集合和源文件指纹均参与证据检查；历史测试数量不能代替当前提交的验证结果。

## 阅读入口

| 文档 | 内容 |
| --- | --- |
| [运行结构](docs/architecture/combined-runtime.md) | 统一业务 JVM、受限入口、SSE 和后台调度 |
| [数据结构](docs/architecture/database-schema-review.md) | 当前表职责与需要保留的权威边界 |
| [数据库访问](docs/architecture/database-access-review.md) | 批量读取、轮询退避、续租和共享锁 |
| [用户画像与证据记忆](docs/architecture/user-profile-memory.md) | Markdown 编辑、来源证据、版本和隐私约束 |
| [Java 规范与逻辑注释](docs/architecture/java-coding-guidelines.md) | 阿里手册参考、项目约定和验证入口 |
| [运行维护](docs/operations.md) | 配置、排空、备份及隔离隐私恢复 |
| [历史记录](docs/history/README.md) | 过去设计与验收证据，仅供追溯 |

代码入口为 `api` → `app` → `domain` 与应用端口；`infrastructure` 提供 MyBatis、AgentScope 和远程适配。`bootstrap` 显式装配业务上下文，`channel` 处理飞书，`worker` 负责执行调度。关键类、接口与复杂方法包含职责、锁顺序、事务和失败处理的中文注释。
