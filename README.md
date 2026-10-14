# FinanceClaw Java

FinanceClaw 从 Python / LangChain / LangGraph 迁移到 **Java / AgentScope Java** 的目标项目。

当前实现范围为 **M0：Java 工程、固定工具链与框架适配实验**。包含 Spring Boot 启动和就绪检查、PostgreSQL 状态存储、独立 JVM 审批/澄清恢复、并发隔离及故障注入测试。产品 Turn API、完整授权、MCP、飞书和长期记忆按后续里程碑实施。

## 固定工具链

| 组件 | 版本 |
| --- | --- |
| JDK | Eclipse Temurin **21.0.12+8** |
| Spring Boot | **3.5.16** |
| AgentScope Java core / OpenAI extension | **2.0.3** |
| MyBatis Spring Boot Starter | **3.0.5** |
| Maven / Wrapper | **3.9.11 / 3.3.4** |

[版本锁](toolchains.lock.json)保存 JDK 分发包的 URL 和 SHA-256；Maven Wrapper 校验分发包，Enforcer 拒绝不符合要求的 JDK 补丁、build、vendor 和 Maven 版本。项目使用本地 `.tools/` 和 `.cache/`，不修改系统 JDK。

构建脚本使用项目内 Maven Central 配置，避免依赖 POM 带入 snapshot/milestone 仓库；不修改用户的全局 Maven 设置。

## 构建与验证

需要 Python 3.12+（仅用于工具链安装及证据整理）、curl、网络，以及已运行的 Docker。默认测试不需要模型 API Key，也不访问 MCP 或飞书。

```bash
# 首次安装；已安装到 .tools/jdk 时跳过
python3 scripts/bootstrap-jdk.py

# 编译、单元测试、真实 PostgreSQL + 独立 JVM 实验、打包和格式检查
scripts/mvn.sh -B -ntp verify spotless:check

# 整理本次测试结果及源码指纹
python3 scripts/collect-m0-evidence.py
```

成功时 Maven 输出 `BUILD SUCCESS`，可执行包位于 `target/financeclaw-java-0.1.0-SNAPSHOT.jar`，证据汇总位于 `target/m0/results.json`。`scripts/mvn.sh test` 仅执行不依赖 PostgreSQL 的单元/HTTP 契约测试，不能代替 M0 完整验证。

## 启动服务骨架

使用独立开发数据库，设置以下环境变量后启动；Flyway 会创建 `agent_runtime` schema：

```bash
export FINANCECLAW_DATABASE_URL=jdbc:postgresql://localhost:5432/financeclaw_java
export FINANCECLAW_DATABASE_USER=financeclaw
# FINANCECLAW_DATABASE_PASSWORD 由本地环境或密钥管理注入
scripts/mvn.sh spring-boot:run
```

另一个终端执行 `curl --fail http://localhost:8080/actuator/health/readiness`，预期为 `{"status":"UP"}`。目前服务只开放健康信息；Agent 实验从测试入口调用，尚无业务聊天、审批或任务 API。

## 阅读与开发入口

| 入口 | 内容 |
| --- | --- |
| [M0 验证记录](docs/migration/m0-validation.md) | 实际版本、测试证据、状态保存边界与继续实施决策 |
| [迁移方案](docs/migration/agentscope-java-migration.md) | 源系统、能力映射、目标架构及分阶段实施 |
| [验收清单](docs/migration/acceptance-checklist.md) | 已完成范围与后续阶段的验收条件 |
| [Agent 适配器](src/main/java/com/financeclaw/agent) | 独立 engine、恢复输入、执行入口、发送前预算检查 |
| [状态与执行保护](src/main/java/com/financeclaw/infrastructure/state) | 原子 CAS、执行互斥、工具结果核对实验 |
| [MyBatis Mapper](src/main/java/com/financeclaw/infrastructure/state/RuntimeMapper.java) | 统一管理状态、工具账本及互斥 SQL；Spring Boot 自动配置连接与映射 |
| [M0 实验](src/test/java/com/financeclaw/m0) | SDK、PostgreSQL、子进程、故障及隔离场景 |
| [数据库迁移](src/main/resources/db/migration/V1__m0_agent_state.sql) | AgentState 与工具账本结构 |

常见问题：

- **JDK 被拒绝**：检查显式 `JAVA_HOME` 是否指向 Temurin 21.0.12+8；未设置时脚本优先使用项目 JDK。
- **Docker 环境不可用**：启动 Docker 并运行 `docker info`；集成测试会失败，不会跳过后冒充通过。
- **数据库连接失败**：检查开发库地址、用户和密码；集成测试使用临时容器，不需要配置这些变量。
- **格式检查失败**：运行 `scripts/mvn.sh spotless:apply` 后重试。

原项目为 [westbrook0904/FinanceClaw](https://github.com/westbrook0904/FinanceClaw)。Python 源码和已有未提交修改保持不变；迁移基线与补丁保存在 [baseline](docs/migration/baseline/python-manifest.json)。
