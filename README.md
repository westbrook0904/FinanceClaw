# FinanceClaw Java

FinanceClaw 从 Python / LangChain / LangGraph 迁移到 **Java / AgentScope Java** 的目标项目。

当前已实现 **M0–M4 的本地能力及 M5 部署基础**：任务生命周期、原生事件与 SSE、授权与审批恢复、结果归档、请求预算，以及通过 AgentScope 原生 Toolkit / SkillRepository 接入的酒店/Taibu MCP、调酒 Skill、市场研究子 Agent 和固定投资组合 Workflow。M4 增加受治理长期记忆、独立受限 Worker，以及 AgentScope 原生飞书文本接入。M6a 已冻结紫微回归基准并迁移日期契约；M6b 已实现固定 Java 历法、本命与流运算法，并完成旧盘面逐字段比对；M6c 已通过 AgentScope 原生 Toolkit 接入五个紫微工具及出生资料澄清、归属、事实分段和结果预算，默认关闭，仅允许显式开发模式。M6d 已增加独立进程故障/竞争、无 Python 容器和真实模型验收入口，真实模型验收仍待配置。默认离线运行；真实供应商、飞书租户与生产交付仍待验收。SDK 暂不支持的卡片/表单和可靠投递回执没有自建实现。

## 固定工具链

| 组件 | 版本 |
| --- | --- |
| JDK | Eclipse Temurin **21.0.12+8** |
| Spring Boot | **3.5.16** |
| AgentScope Java core / OpenAI / RAG / Feishu / Harness | **2.0.3** |
| MyBatis Spring Boot Starter | **3.0.5** |
| Maven / Wrapper | **3.9.11 / 3.3.4** |

[版本锁](toolchains.lock.json)保存 JDK 分发包的 URL 和 SHA-256；Maven Wrapper 校验分发包，Enforcer 拒绝不符合要求的 JDK 补丁、build、vendor 和 Maven 版本。项目使用本地 `.tools/` 和 `.cache/`，不修改系统 JDK。

构建脚本使用项目内 Maven Central 配置，避免依赖 POM 带入 snapshot/milestone 仓库；不修改用户的全局 Maven 设置。

## 构建与验证

需要 Python 3.12+（仅用于工具链安装及证据整理）、curl、网络，以及已运行的 Docker。默认测试不需要模型 API Key，仅访问本地模拟模型、embedding、MCP 和飞书端点，不访问真实服务。

```bash
# 首次安装；已安装到 .tools/jdk 时跳过
python3 scripts/bootstrap-jdk.py

# 编译、单元测试、真实 PostgreSQL + 独立 JVM 实验、打包和格式检查
scripts/mvn.sh -B -ntp verify spotless:check

# 整理本次测试结果及源码指纹
python3 scripts/test-live-acceptance.py
python3 scripts/collect-evidence.py
```

成功时 Maven 输出 `BUILD SUCCESS`，可执行包位于 `target/financeclaw-java-0.1.0-SNAPSHOT.jar`，证据汇总位于 `target/m6/results.json`。`scripts/mvn.sh test` 仅执行不依赖 PostgreSQL 的单元/HTTP 契约测试，不能代替包含 PostgreSQL 和业务 API 的完整验证。

## 容器部署

```bash
python3 scripts/init-local.py
scripts/mvn.sh -B -ntp package -DskipTests
docker compose build api
docker compose up -d --wait --wait-timeout 180
```

默认使用新数据库与离线模型，API 仅绑定本机；迁移完成后启动 API、Worker、Integrations、受限 Memory Worker。凭据由 Spring configtree 读取本地挂载的 secret。配置、备份恢复及验收边界见 [M5 交付说明](docs/migration/m5-validation.md)。

## 启动与试用

使用独立开发数据库，设置以下环境变量后启动；Flyway 会创建 `agent_runtime`、`business`、`memory` 和 `channel` schema：

```bash
export FINANCECLAW_DATABASE_URL=jdbc:postgresql://localhost:5432/financeclaw_java
export FINANCECLAW_DATABASE_USER=financeclaw
# FINANCECLAW_DATABASE_PASSWORD 和 FINANCECLAW_API_TOKEN 由本地环境或密钥管理注入
export FINANCECLAW_TENANT=local
export FINANCECLAW_SUBJECT=developer
scripts/mvn.sh spring-boot:run
```

另一个终端执行 `curl --fail http://localhost:8080/actuator/health/readiness`，预期为 `{"status":"UP"}`。业务 API 使用 `Authorization: Bearer $FINANCECLAW_API_TOKEN`；未设置 token 时返回 401。创建会话、提交消息、观察 SSE 和取消的可复制示例见 [M1 使用与验证](docs/migration/m1-validation.md)。默认离线回复为 `已收到：<消息>`。

## 阅读与开发入口

| 入口 | 内容 |
| --- | --- |
| [M5 部署与恢复](docs/migration/m5-validation.md) | 四角色 Compose、secret 配置、CI、故障/备份实验与真实验收入口 |
| [M6 紫微迁移](docs/migration/m6-validation.md) | M6a 日期契约、[M6b 历法](docs/migration/m6b-calendar.md)与[完整排盘比对](docs/migration/m6b-charts.md)；[M6c 领域接入](docs/migration/m6c-domain.md)、[M6d 恢复与部署](docs/migration/m6d-recovery.md)，真实模型验收待完成 |
| [M4 记忆与原生飞书](docs/migration/m4-validation.md) | 记忆 API、独立 Worker、pgvector、原生飞书配置及 SDK 能力边界 |
| [M3 业务与验证](docs/migration/m3-validation.md) | 酒店/Taibu 配置、调酒 Skill、市场研究、固定 Workflow 及模拟验收 |
| [M2 使用与验证](docs/migration/m2-validation.md) | 授权与交互 API、Artifact、模型配置、测试和限制 |
| [框架复用审查](docs/migration/framework-reuse-review.md) | 原生能力的复用方式及保留业务适配的原因 |
| [M1 使用与验证](docs/migration/m1-validation.md) | API 示例、恢复边界、配置及验收证据 |
| [M0 验证记录](docs/migration/m0-validation.md) | 实际版本、测试证据、状态保存边界与继续实施决策 |
| [迁移方案](docs/migration/agentscope-java-migration.md) | 源系统、能力映射、目标架构及分阶段实施 |
| [验收清单](docs/migration/acceptance-checklist.md) | 已完成范围与后续阶段的验收条件 |
| [Agent 适配器](src/main/java/com/financeclaw/agent) | 独立 engine、恢复输入、执行入口、发送前预算检查 |
| [状态与执行保护](src/main/java/com/financeclaw/infrastructure/state) | 原子 CAS、执行互斥、工具结果核对实验 |
| [MyBatis Mapper](src/main/java/com/financeclaw/infrastructure/state/RuntimeMapper.java) | 统一管理状态、工具账本及互斥 SQL；Spring Boot 自动配置连接与映射 |
| [任务事务与恢复](src/main/java/com/financeclaw/application/TaskService.java) | 幂等、领取、租约、取消、完成回执与 Outbox |
| [后台执行](src/main/java/com/financeclaw/worker/TaskWorker.java) | 有界调度、定向中断和恢复轮询 |
| [M1 集成测试](src/test/java/com/financeclaw/m1/TaskLifecycleIT.java) | API、真实数据库竞争、独立 JVM 崩溃恢复 |
| [M0 实验](src/test/java/com/financeclaw/m0) | SDK、PostgreSQL、子进程、故障及隔离场景 |
| [数据库迁移](src/main/resources/db/migration) | AgentState、工具账本与任务生命周期结构 |

常见问题：

- **JDK 被拒绝**：检查显式 `JAVA_HOME` 是否指向 Temurin 21.0.12+8；未设置时脚本优先使用项目 JDK。
- **Docker 环境不可用**：启动 Docker 并运行 `docker info`；集成测试会失败，不会跳过后冒充通过。
- **数据库连接失败**：检查开发库地址、用户和密码；集成测试使用临时容器，不需要配置这些变量。
- **格式检查失败**：运行 `scripts/mvn.sh spotless:apply` 后重试。

原项目为 [westbrook0904/FinanceClaw](https://github.com/westbrook0904/FinanceClaw)。Python 源码和已有未提交修改保持不变；迁移基线与补丁保存在 [baseline](docs/migration/baseline/python-manifest.json)。
