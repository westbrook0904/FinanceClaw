# FinanceClaw Java

FinanceClaw 从 Python / LangChain / LangGraph 迁移到 **Java / AgentScope Java** 的目标项目。

当前已实现 **M0–M4 的本地能力及 M5 部署基础**：任务生命周期、原生事件与 SSE、授权与审批恢复、结果归档、请求预算，以及通过 AgentScope 原生 Toolkit / SkillRepository 接入的酒店/Taibu MCP、调酒 Skill、市场研究子 Agent 和固定投资组合 Workflow。M4 增加受治理长期记忆、独立受限 Worker，以及 AgentScope 原生飞书文本接入。M6a 已冻结紫微回归基准并迁移日期契约；M6b 已实现固定 Java 历法、本命与流运算法，并完成旧盘面逐字段比对；M6c 已通过 AgentScope 原生 Toolkit 接入五个紫微工具及出生资料澄清、归属、事实分段和结果预算，默认关闭，仅允许显式开发模式。M6d 已增加独立进程故障/竞争、无 Python 容器和真实模型验收入口，真实模型验收仍待配置。默认离线运行；真实供应商、飞书租户与生产交付仍待验收。SDK 暂不支持的卡片/表单和可靠投递回执没有自建实现。

最新开发补充 **[M5b 受理暂停与排空检查](docs/migration/m5b-admission.md)**：暂停当前认证主体的新任务，保留已有任务的审批、取消和恢复；检查任务、通知、记忆及隐私积压，受理状态跨重启和备份恢复保留。M6d 已本地提交为 `72e0655`，未推送。

M5b 已本地提交 `07a9bfb`。后续 **[M5c 排空阻塞项定位](docs/migration/m5c-backlog.md)** 增加按主体隔离的分页明细，可定位任务、通知、未投影绑定和记忆作业，查询不产生业务写入。

M5c 已本地提交 `3736aac`。**[M5d 只读预检报告](docs/migration/m5d-preflight.md)** 将受理状态、计数与分页明细保存为本地证据，区分仍有阻塞、检查不完整和可供人工复核，不自动切换服务。

M5d 已本地提交 `993e7ac`，未推送。**[M5e 旧备份隐私核对](docs/migration/m5e-restore-privacy.md)** 检查恢复库是否遗漏备份之后的遗忘事实，并在隔离容器中演练拦截旧备份；真实环境交付和线上切换仍待验收。

M5e 与 R0–R5a 已保存为本地检查点 `bb58ea2`，[R5b 紫微边界拆分](docs/architecture/r5b-implementation.md) 与 [R5c 记忆与发布分层](docs/architecture/r5c-implementation.md) 已提交为本地检查点 `7e5a903`，未推送。后续 **[R6 角色装配](docs/architecture/r6-implementation.md)** 已完成：各角色显式加载组件与 Mapper，Outbox 投影独立运行；完整回归 835 项、隔离 Compose 27 项检查通过，三个启动根的包图无循环。R6 已提交为本地检查点 `43d0c53`，未推送；继续保留单 Maven 模块和既有启动入口。

[治理与飞书入口依赖收缩](docs/architecture/entrypoint-decoupling.md)已保存为本地检查点 `0c549fa`，未推送；该批通过 630 项单元/HTTP/架构测试，数据库集成被本机 Docker API 500 阻断。

随后[原生执行适配依赖收缩](docs/architecture/runtime-decoupling.md)已移除生产 TaskService，原生 Runtime/Tools 直接使用现有服务与端口，Worker 不再构造投影用例及网关；测试委托移入显式导入的 TaskFixture。该批通过 630 项单元/HTTP/架构测试及 1 项不依赖 Docker 的产品打包检查，包含探针字节码依赖门禁；这些结果保留为该批历史证据。

最新[治理用例分层](docs/architecture/governance-decoupling.md)已删除生产 GovernanceService，将授权、Artifact 和报告发布分别交给 GrantService、ArtifactService、ReportService，HTTP 回答直接调用 AnswerInteractionService。API 不装配报告发布服务，Worker 不装配授权变更服务，Integrations 不装配这两类服务；旧 application 生产包已清空。最新两批改动已保存为本地检查点 `4f4f7b4`，未推送。本批 685 项单元/HTTP/架构测试及 1 项产品打包检查通过；Docker API 仍返回 HTTP 500，数据库、角色实际启动、独立进程恢复与 Compose 尚未重跑，不能沿用此前批次的测试结果作为本批验收。

继续完成的[HTTP 请求类型化与 Bean Validation](docs/architecture/http-request-validation.md)已为任务提交和受理状态变更接入 Boot validation starter、类型化请求及严格 Jackson 映射，保留原错误码与幂等校验顺序。本批 821 项单元/HTTP/架构测试及 1 项产品打包检查通过，新增 136 项测试；改动尚未提交。Docker 相关集成验证仍待恢复后完成。

最新[记忆接口 Bean Validation](docs/architecture/memory-http-validation.md)已将确认/拒绝与遗忘请求的静态判断交给标准约束，保留原 Jackson 绑定、错误码、功能开关与幂等顺序。全量 906 项单元/HTTP/架构测试及 1 项产品打包检查通过，本批新增 85 项测试。两批 HTTP 校验改动均未提交，数据库与恢复验证仍受 Docker API 500 阻断。

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
python3 scripts/test-inspect-drain.py
python3 scripts/test-restore-privacy.py
python3 scripts/collect-evidence.py
```

成功时 Maven 输出 `BUILD SUCCESS`，可执行包位于 `target/financeclaw-java-0.1.0-SNAPSHOT.jar`，证据汇总位于 `target/m5e/results.json`。`scripts/mvn.sh test` 仅执行不依赖 PostgreSQL 的单元/HTTP 契约测试，不能代替包含 PostgreSQL 和业务 API 的完整验证。

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
| [M5b 暂停与排空](docs/migration/m5b-admission.md) | 按认证主体暂停/恢复受理、并发边界、任务/渠道/记忆积压及恢复演练 |
| [M5c 阻塞项定位](docs/migration/m5c-backlog.md) | 只读分页、原任务/通知定位、记忆作业和查询权限边界 |
| [M5d 只读预检](docs/migration/m5d-preflight.md) | CLI 留档、前后观测核对、分页完整性与退出码 |
| [M5e 旧备份隐私核对](docs/migration/m5e-restore-privacy.md) | 全主体隐私快照、遗忘审计与残留检查、旧库隔离演练 |
| [M6 紫微迁移](docs/migration/m6-validation.md) | M6a 日期契约、[M6b 历法](docs/migration/m6b-calendar.md)与[完整排盘比对](docs/migration/m6b-charts.md)；[M6c 领域接入](docs/migration/m6c-domain.md)、[M6d 恢复与部署](docs/migration/m6d-recovery.md)，真实模型验收待完成 |
| [M4 记忆与原生飞书](docs/migration/m4-validation.md) | 记忆 API、独立 Worker、pgvector、原生飞书配置及 SDK 能力边界 |
| [M3 业务与验证](docs/migration/m3-validation.md) | 酒店/Taibu 配置、调酒 Skill、市场研究、固定 Workflow 及模拟验收 |
| [M2 使用与验证](docs/migration/m2-validation.md) | 授权与交互 API、Artifact、模型配置、测试和限制 |
| [框架复用审查](docs/migration/framework-reuse-review.md) | 原生能力的复用方式及保留业务适配的原因 |
| [AgentScope 与 COLA 整改方案](docs/architecture/agentscope-cola-remediation.md) | 重复实现核查、依赖环、目标包结构、领域对象与 R0–R7 整改顺序 |
| [R0–R2 实施记录](docs/architecture/r0-r2-implementation.md) | 固定契约、M0 测试隔离、Operations 分层、Spring 事务与实际回归结果 |
| [R3 交互与工具边界](docs/architecture/r3-implementation.md) | 回答用例、领域绑定、统一 SDK 快照、业务工具贡献与事务回归 |
| [R4 任务与执行分层](docs/architecture/r4-implementation.md) | 任务用例、原生执行端口、Spring 原子性、恢复与取消边界 |
| [R5a 组合与市场研究](docs/architecture/r5a-implementation.md) | 领域规则、业务步骤、原生驱动分离；发布幂等、子预算与取消回归 |
| [R5b 紫微执行与 I/O](docs/architecture/r5b-implementation.md) | 输入、资源、密钥、计算与原生工具分离；旧回执、事务回滚和零循环门禁 |
| [R5c 记忆与发布分层](docs/architecture/r5c-implementation.md) | 记忆领域规则、模型/持久化端口、不可变发布值与凭据分离；真实回滚、旧回执和隐私竞态验证 |
| [R6 角色装配](docs/architecture/r6-implementation.md) | 各角色 Bean/Mapper 清单、独立投影、真实启动与恢复验证、Maven 模块决定 |
| [入口与原生执行依赖收缩](docs/architecture/runtime-decoupling.md) | 删除生产 TaskService、缩小 Worker 装配、隔离测试夹具与该批产品打包检查 |
| [治理用例分层](docs/architecture/governance-decoupling.md) | 删除生产 GovernanceService，分离授权、Artifact、报告发布和 HTTP 回答，收窄各角色装配 |
| [HTTP 请求类型化与 Bean Validation](docs/architecture/http-request-validation.md) | 任务提交与受理变更的静态 DTO、严格 Jackson 映射、原错误顺序与兼容性验证 |
| [记忆接口 Bean Validation](docs/architecture/memory-http-validation.md) | 确认/拒绝与遗忘请求的标准静态约束、既有错误码及隐私/幂等顺序 |
| [Spring 事务与组件复审](docs/architecture/spring-transactions-component-review.md) | Spring 统一事务、SDK 主动提交实验证据、已接入 ArchUnit 与组件复用清单 |
| [M1 使用与验证](docs/migration/m1-validation.md) | API 示例、恢复边界、配置及验收证据 |
| [M0 验证记录](docs/migration/m0-validation.md) | 实际版本、测试证据、状态保存边界与继续实施决策 |
| [迁移方案](docs/migration/agentscope-java-migration.md) | 源系统、能力映射、目标架构及分阶段实施 |
| [验收清单](docs/migration/acceptance-checklist.md) | 已完成范围与后续阶段的验收条件 |
| [Agent 适配器](src/main/java/com/financeclaw/infrastructure/agentscope) | 原生执行、恢复、业务工具适配与发送前预算检查 |
| [状态适配](src/main/java/com/financeclaw/infrastructure/state) | AgentState 版本 CAS、身份隔离与墓碑 |
| [MyBatis 状态 Mapper](src/main/java/com/financeclaw/infrastructure/state/RuntimeMapper.java) | AgentState SQL；连接与事务由 Spring/MyBatis-Spring 管理 |
| [任务用例](src/main/java/com/financeclaw/app/task) / [执行用例](src/main/java/com/financeclaw/app/execution) | 受理、查询、取消、租约、完成回执、恢复与 Outbox |
| [授权用例](src/main/java/com/financeclaw/app/governance) / [Artifact 用例](src/main/java/com/financeclaw/app/artifact) / [报告用例](src/main/java/com/financeclaw/app/report) | 授权变更与重放、归档与访问核对、本地报告效果回执 |
| [记忆用例](src/main/java/com/financeclaw/app/memory) / [发布用例](src/main/java/com/financeclaw/app/release) | 事实与隐私治理、事务外模型调用、固定发布版本与配置适配 |
| [角色装配](src/main/java/com/financeclaw/bootstrap/RoleConfiguration.java) | Spring 显式导入各角色组件，精确注册 Mapper |
| [后台执行](src/main/java/com/financeclaw/worker/TaskWorker.java) / [独立投影](src/main/java/com/financeclaw/worker/OutboxProjector.java) | Worker 有界执行与恢复；Integrations 消费 Outbox |
| [M1 集成测试](src/test/java/com/financeclaw/m1/TaskLifecycleIT.java) | API、真实数据库竞争、独立 JVM 崩溃恢复 |
| [M0 实验](src/test/java/com/financeclaw/m0) | SDK、PostgreSQL、子进程、故障及隔离场景 |
| [数据库迁移](src/main/resources/db/migration) | AgentState、工具账本与任务生命周期结构 |

常见问题：

- **JDK 被拒绝**：检查显式 `JAVA_HOME` 是否指向 Temurin 21.0.12+8；未设置时脚本优先使用项目 JDK。
- **Docker 环境不可用**：启动 Docker 并运行 `docker info`；集成测试会失败，不会跳过后冒充通过。
- **数据库连接失败**：检查开发库地址、用户和密码；集成测试使用临时容器，不需要配置这些变量。
- **格式检查失败**：运行 `scripts/mvn.sh spotless:apply` 后重试。

原项目为 [westbrook0904/FinanceClaw](https://github.com/westbrook0904/FinanceClaw)。Python 源码和已有未提交修改保持不变；迁移基线与补丁保存在 [baseline](docs/migration/baseline/python-manifest.json)。
