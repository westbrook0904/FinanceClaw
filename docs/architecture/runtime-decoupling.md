# R6 后续：移除旧任务入口，收窄原生执行依赖

实施日期：2026-10-20。上一批[治理与飞书入口依赖收缩](entrypoint-decoupling.md)已保存为本地检查点 `0c549fa`，未推送。本文记录后续开发：生产 `TaskService` 已删除，原生执行适配直接调用已有用例和端口，测试兼容入口仅存在于测试目录。本批与后续治理拆分已一同提交为本地检查点 `4f4f7b4`，未推送。本文保留该批实现与验证结果；后续[治理用例分层](governance-decoupling.md)已删除 GovernanceService，并进一步收窄角色装配。两批检查点为 `4f4f7b4`，本文的 630 项测试与 1 项打包检查不代表后续批次已通过验收。

## 该批完成时的调用关系

| 调用者 | 直接依赖 | 原有职责的归属 |
| --- | --- | --- |
| [TaskAgentRuntime](../../src/main/java/com/financeclaw/infrastructure/agentscope/execution/TaskAgentRuntime.java) | ExecutionPolicyService、ExecutionLeaseService、ReleaseService、Credentials、ExecutionContextGateway | 策略/预算/历史、租约保护、固定发布、凭据、恢复交互 |
| [GovernedTools](../../src/main/java/com/financeclaw/infrastructure/agentscope/execution/GovernedTools.java) | ExecutionPolicyService、ReleaseService、Credentials | 工具权限、消耗预算、固定发布、收窄子 Agent 的 scopes |
| [McpToolContribution](../../src/main/java/com/financeclaw/infrastructure/agentscope/capability/McpToolContribution.java) | ExecutionPolicyService、Credentials | 注册前核对策略，远程请求时解析凭据引用 |

该批完成时，三个调用者仍通过 GovernanceService 使用 Artifact 和本地报告能力。后续[治理用例分层](governance-decoupling.md)已将这些依赖改为 ArtifactService 和 ReportService，MCP 仅依赖 ArtifactService。原生 Agent、Toolkit、SkillRepository、MCP 客户端和模型仍使用固定版本的 AgentScope；没有新建聚合 facade、执行框架或通用事务包装。

旧 `application.TaskService` 只有委托方法，没有事务拦截、SDK 状态或 SQL 所有权。删除它后，业务操作仍进入原用例：

- SDK 状态保存与执行结束后的策略复核仍在 `ExecutionLeaseService.fenced` 中进行，核对当前 attempt、租约和 fence。
- 模型请求仍在预算短事务结束后执行；工具调用仍先核对授权、隐私与实际输入，再消耗预算。
- 恢复仍核对已回答交互、原 attempt、发布版本和保存版本；只把交互读取转到已有 ExecutionContextGateway。
- 子 Agent 的权限仍不得超出父工具集合，并再次与当前授权和固定发布的 scopes 取交集。
- MCP 继续用原生传输和 Schema，凭据仍在请求时解析；归档、结果预算与业务回执实现未变。

上述是代码差异与静态审查结论。本批没有完成 PostgreSQL 运行验证，不能据此宣称故障恢复行为已重新验收。

## 角色装配与测试隔离

[RoleConfiguration](../../src/main/java/com/financeclaw/bootstrap/RoleConfiguration.java) 不再装配已删除的 facade。Worker 的 Execution 配置也不再导入投影用例和持久化适配；Projection 只由 Delivery 引入，供 Integrations 和 Standalone 使用。Worker 完成任务时仍通过 TaskTransitionService 在原事务写入 Outbox，由 Integrations 的 OutboxProjector 消费。

[RoleAssemblyIT](../../src/test/java/com/financeclaw/m5/RoleAssemblyIT.java) 增加 Worker 缺少 ProjectTaskService、MyBatisTaskProjectionGateway 的断言，并检查各角色产品 Bean 中没有旧 facade 或测试 fixture。这些数据库角色测试本轮仅完成编译，实际启动仍待 Docker 恢复。

存量集成测试通过显式 `@Import` 或手动构造使用 [TaskFixture](../../src/test/java/com/financeclaw/fixture/TaskFixture.java)。它保留原测试驱动方法，减少本次构造器迁移对既有场景的干扰；位于独立测试包，不作为生产组件扫描或产品依赖。

[ZiweiRuntimeProbe](../../src/test/java/com/financeclaw/m6/ZiweiRuntimeProbe.java) 需要被单独打成单类探针，因此直接取得生产策略、租约、完成与恢复服务，不引用 TaskFixture。测试辅助入口与可独立运行的恢复探针保持不同的依赖边界。

## 该批已完成的验证

```bash
scripts/mvn.sh -B -ntp spotless:apply clean package spotless:check \
  failsafe:integration-test failsafe:verify -Dit.test=RuntimePackagingIT

python3 scripts/check-delivery.py
```

该批执行第一条命令时不需要 Docker，覆盖当时全部 630 项单元、HTTP 与架构测试，以及 1 项产品打包检查；全部通过，零失败、零错误、零跳过。Spotless 格式检查通过。对应证据保留原样；后续治理分层的新增测试与构造器变更以[新批次记录](governance-decoupling.md)为准。

[ArchitectureTest](../../src/test/java/com/financeclaw/architecture/ArchitectureTest.java) 保留三个启动根的零循环门禁，并禁止生产代码重新依赖旧 TaskService 或测试 fixture。

[RuntimePackagingIT](../../src/test/java/com/financeclaw/architecture/RuntimePackagingIT.java) 从实际 Boot Jar 提取依赖，以平台类加载器为父级，只加入产品类和单个恢复探针：

1. 检查产品 Jar 不含旧 facade、fixture 或 M6 测试类。
2. 加载三个原生适配类，并构造单类探针，避免 Maven 测试 classpath 掩盖缺失依赖。
3. 用现有 ArchUnit 检查探针字节码的直接依赖，不允许方法体引用其他测试类，补足 JVM 延迟解析的盲区。

这项检查验证产品打包和探针依赖边界，没有执行数据库恢复或真实模型调用。受保护的 110 个 SQL、资源、算法、领域、脚本及构建文件指纹不变，依赖版本没有升级。测试报告摘要、源码指纹和本批范围见[验证证据](evidence/runtime-decoupling.json)。

## 待完成的验证与开发

本机 Docker API 仍返回 HTTP 500，数据库、角色启动、独立进程故障恢复和 Compose 检查没有重跑。此前 R6 的 835 项完整回归及 27 项 Compose 检查是历史基线，不代表本批已通过相同验收。Docker 只做了只读检查，未重启或修改配置。

Docker 恢复后执行：

```bash
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/check-delivery.py
```

随后按 [R6 实施记录](r6-implementation.md) 重跑隔离 Compose 验证，并更新证据。真实供应商、飞书租户和生产切换继续按各自验收入口处理。

旧任务 facade 的生产调用者已清零。后续[治理用例分层](governance-decoupling.md)也已删除 GovernanceService，授权、Artifact 和报告发布分别使用独立应用服务与持久化端口，HTTP 回答直接进入已有回答用例。两批改动已本地提交，仍待数据库、角色启动与恢复验证。R7 原生替换实验继续按等价性证据决定。
