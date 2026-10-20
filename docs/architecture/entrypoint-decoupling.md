# R6 后续：治理与飞书入口的依赖收缩

实施日期：2026-10-20。基于本地检查点 `43d0c53`（R6 角色装配，未推送），继续移除入口对旧 TaskService 的依赖。本批实现已完成，验证结果见下文；新增改动尚未提交。

## 当前调用关系

| 入口 | 直接依赖的用例 | 保留的业务边界 |
| --- | --- | --- |
| GovernanceService | TaskQueryService、ExecutionLeaseService、ExecutionPolicyService、ReleaseService、AnswerInteractionService | owner、fence、策略与隐私核对、固定发布、回答回执 |
| FeishuService | TaskQueryService、SubmitTaskService、CancelTaskService、AnswerInteractionService | 绑定归属、受理、取消、审批/澄清、回调去重 |

[GovernanceService](../../src/main/java/com/financeclaw/application/GovernanceService.java) 不再引用 TaskService。Artifact/报告仍在 `fenced → checkPolicy → 写入` 中执行；Artifact 读取仍先核对 owner/privacy，再锁 grant，最后核对原发布与内容 hash。原 TransactionTemplate、SQL 与嵌套事务位置不变。

[FeishuService](../../src/main/java/com/financeclaw/channel/FeishuService.java) 不再引用 TaskService 或 GovernanceService，直接调用已有受理、查询、取消和回答用例。回调外层事务仍覆盖业务变更与 inbox 回执；没有新增任务代理、重试或另一套回执实现。原生消息映射继续使用 AgentScope，发送能力仍属于 Integrations。

## API 角色进一步收窄

[RoleConfiguration](../../src/main/java/com/financeclaw/bootstrap/RoleConfiguration.java) 将旧 facade、完成、恢复、执行回执适配从共享配置移至 Execution；将投影用例独立为 Projection 配置，仅由 Execution 与 Delivery 引入。

API 不再装配以下六个 Bean：

- TaskService
- ProjectTaskService、MyBatisTaskProjectionGateway
- ExecutionCompletionService、RecoverTaskService、MyBatisExecutionReceiptGateway

查询、受理、取消、授权、回答、Artifact 与记忆功能所需的服务仍存在。API 与 Worker 继续共享策略、租约及转换服务，因为治理操作也需要这些能力；本批不宣称 API 已不存在所有执行相关用例。

Worker 仍为三个原生适配调用者保留 TaskService，因此仍构造投影用例；定时 OutboxProjector 继续只由 Integrations/Standalone 加载。Mapper 集合、启动类、产品 Jar、数据库权限、SQL、算法、固定样本和依赖版本均不变。

## 验证

[ArchitectureTest](../../src/test/java/com/financeclaw/architecture/ArchitectureTest.java) 新增入口依赖门禁：API/channel 与 GovernanceService 不得重新引用旧 TaskService，FeishuService 不得引用旧 application 包。原有零循环和 Spring 事务规则保留。

[RoleAssemblyIT](../../src/test/java/com/financeclaw/m5/RoleAssemblyIT.java) 增加 API 上述六个 Bean 的实际缺席断言，并新增真实 HTTP/PostgreSQL 场景：授权更新与原回执重放、同 key 异请求冲突、跨主体访问拒绝、排队任务取消与取消回执重放、飞书通知查询、API 不自行消费 Outbox。既有 API/Worker/Integrations 分离执行流程仍参与回归。

```bash
scripts/mvn.sh -B -ntp test spotless:check

# 需要正常响应的 Docker Engine
scripts/mvn.sh -B -ntp package \
  -Dtest=ArchitectureTest,InteractionHttpContractTest,DeploymentAccessTest \
  failsafe:integration-test failsafe:verify \
  -Dit.test=RoleAssemblyIT,GovernanceIT,FeishuIT

scripts/mvn.sh -B -ntp clean verify spotless:check
```

当前已通过 630 项单元/HTTP/架构测试（22 个测试套件，含 17 项 ArchUnit），零失败、零错误、零跳过，Spotless 格式检查通过。首次数据库集成启动被 Docker API HTTP 500 阻断，尚未完成；不能将该次结果记录为代码回归通过。受保护的 110 个 SQL、资源、算法、领域、脚本与构建输入指纹未变。

Docker 只读诊断定位为主机代理无法到达 Linux VM 的路由；故障自 10 月 19 日已存在，早于本轮代码改动。重启 Docker Desktop 可能中断其他任务的容器，当前等待用户授权，未修改 Docker 配置或数据。后续需在 Docker 恢复后重跑上面的集成与完整验证命令。

本轮源码指纹、测试套件和未完成项见[验证证据](evidence/entrypoint-decoupling.json)。

## 后续范围

剩余 TaskService 生产调用者为 TaskAgentRuntime、GovernedTools、McpToolContribution。下一切片应直接接入现有策略、租约、发布、凭据与上下文端口，同时迁移独立 ZiweiRuntimeProbe 和测试 fixture，再删除生产 facade。不能把测试辅助类带进产品 Jar，也不为替换 facade 创建另一层聚合代理。

GovernanceService 自身仍承担授权/Artifact 持久化；本批只移除其对 TaskService 的依赖，没有宣称该服务已完成全部分层。R7 原生替换实验保持可选，真实供应商与生产切流仍单独验收。
