# 治理用例分层：授权、Artifact 与报告发布

> **历史记录**：本文保留当时的设计、命令与验证结果，不代表当前独立应用的部署结构或最新测试结论。当前操作见[运行说明](../../operations.md)，新库结构见[初始化说明](../../architecture/fresh-schema.md)；历史类名、版本号和外部导出命令仅用于理解证据。

实施日期：2026-10-20。承接[原生执行适配依赖收缩](runtime-decoupling.md)，本批及上一批原生执行改动已保存为本地检查点 `4f4f7b4`，未推送。下文验证证据记录提交前的同一份构建输入，历史证据文件保持原样。

生产 `GovernanceService` 已删除。授权、Artifact 和报告发布分别进入已有 COLA 分层中的应用服务；回答直接使用已拆出的 AnswerInteractionService。旧 `application` 生产包已清空，没有保留聚合兼容入口。

## 使用与代码入口

| 职责 | 应用用例 | 持久化适配 |
| --- | --- | --- |
| 查询授权、修改 scopes、版本与幂等回执 | [GrantService](../../../src/main/java/com/financeclaw/app/governance/GrantService.java) → GrantGateway | [MyBatisGrantGateway](../../../src/main/java/com/financeclaw/infrastructure/governance/MyBatisGrantGateway.java) |
| 归档、权限/隐私核对、受限回读 | [ArtifactService](../../../src/main/java/com/financeclaw/app/artifact/ArtifactService.java) → ArtifactGateway | [MyBatisArtifactGateway](../../../src/main/java/com/financeclaw/infrastructure/artifact/MyBatisArtifactGateway.java) |
| 本地报告发布及效果回执 | [ReportService](../../../src/main/java/com/financeclaw/app/report/ReportService.java) → ReportGateway | [MyBatisReportGateway](../../../src/main/java/com/financeclaw/infrastructure/governance/MyBatisReportGateway.java) |
| 问题归属、审批/澄清回答、恢复入队 | [AnswerInteractionService](../../../src/main/java/com/financeclaw/app/interaction/AnswerInteractionService.java) | 沿用已有交互适配 |

[GovernanceController](../../../src/main/java/com/financeclaw/api/GovernanceController.java) 直接调用前三类 HTTP 需要的服务：GrantService、ArtifactService、AnswerInteractionService。URL、请求字段、HTTP 状态码与响应形态保持原契约。

原生执行器、GovernedTools 和 LocalToolContribution 使用 ArtifactService 与 ReportService；MCP、组合与紫微归档适配只依赖 ArtifactService。没有新建通用 Repository、事务框架或工具执行器，AgentScope 和 Spring 的使用方式保持原样。

Artifact 的 UTF-8 编码、hash、JSON Pointer 与分页集中在 [ArtifactPayload](../../../src/main/java/com/financeclaw/support/ArtifactPayload.java)。应用服务不直接依赖 Jackson；JSON 字段选择继续使用 Jackson，排序与摘要继续使用既有 PayloadJson/PayloadDigest。原领域 record、冻结 JSON/hash 样本、Mapper SQL 与 Flyway 没有修改。

Artifact 适配位于独立 `infrastructure.artifact` 包，因为它组合任务与治理 Mapper。该位置避免与 `infrastructure.task → infrastructure.governance` 的既有依赖形成环；零循环门禁没有放宽。

## 保留的事务与错误语义

| 操作 | 必须保留的顺序 |
| --- | --- |
| 授权变更 | 幂等键 → owner → scopes → Spring 事务内锁 grant → 原请求回执 → revision → 更新与回执同事务 |
| 普通归档 | 编码及 2 MiB 上限 → fence → 当前策略 → hash 与插入 |
| 带 scope 归档 | 外层 fence/策略/scope → 原普通归档 → scope 写入；嵌套操作加入同一 Spring 事务 |
| Artifact 回读 | owner → 紫微隐私核对/锁 → grant 锁 → 通用及资源 scope → 固定发布 → hash → JSON/path → 分页 |
| 报告发布 | fence → 当前策略 → 原效果回执 → begin effect → report → complete effect |

授权的旧请求先重放原回执，再考虑当前 revision；重复 scopes 仍排序去重后生成原摘要。报告的旧结果原样返回，未知或待核对效果不能因拆分而自动重试。Artifact 保留 16 KiB 回读限制，完整对象不套用列表分页，数组按真实 UTF-8 大小缩短返回窗口。

事务仍由 Spring TransactionTemplate 与 MyBatis-Spring 管理。没有手动 JDBC commit/rollback，也没有把远程模型/MCP 请求移入数据库事务。

## 角色与测试边界

[RoleConfiguration](../../../src/main/java/com/financeclaw/bootstrap/RoleConfiguration.java) 按实际调用装配：

| 运行角色 | GrantService | ArtifactService | ReportService |
| --- | --- | --- | --- |
| API | 有 | 有 | 无 |
| Worker | 无 | 有 | 有 |
| Integrations | 无 | 无 | 无 |
| Standalone | 有 | 有 | 有 |

这是代码装配与待执行断言的范围；本轮尚未运行真实数据库角色启动。[RoleAssemblyIT](../../../src/test/java/com/financeclaw/m5/RoleAssemblyIT.java) 已更新上述存在/缺席断言，继续检查旧 application 类和测试辅助类不得进入产品 Bean。

既有集成场景用显式导入或手动构造的 [GovernanceFixture](../../../src/test/java/com/financeclaw/fixture/GovernanceFixture.java) 调用四个真实用例，测试辅助类不参与产品装配。独立 ZiweiRuntimeProbe 继续直接使用产品依赖，产品 Jar 与单类探针的隔离检查同步更新。

## 验证与证据

```bash
# 不需要 Docker；同时编译全部集成测试
scripts/mvn.sh -B -ntp spotless:apply clean package spotless:check \
  failsafe:integration-test failsafe:verify -Dit.test=RuntimePackagingIT

python3 scripts/check-delivery.py
```

最终通过 685 项单元、HTTP 与架构测试，以及 1 项产品打包检查，零失败、零错误、零跳过；Spotless 格式检查通过。17 项架构门禁包含三个生产启动根的零循环检查。110 个受保护的 SQL、资源、算法、既有领域与构建文件指纹未变。本批验证结果与 326 个构建输入指纹见[机器证据](../../architecture/evidence/governance-decoupling.json)。新增 55 项单元测试：

- GrantServiceTest 11 项：原请求摘要、scope 排序去重、旧回执与错误优先级。
- ReportServiceTest 12 项：旧效果回放、未知结果不重试、租约/授权阻断与 Spring 事务调用。
- ArtifactPayloadTest 20 项：2 MiB/16 KiB 精确 UTF-8 边界、路径、对象与列表契约。
- ArtifactServiceTest 12 项：owner/隐私/grant/发布核对顺序、嵌套 fence 与 scope 写入。

单元测试中持久化端口及部分事务管理器使用 mock，不能证明真实数据库回滚。另新增 [GovernanceBoundariesIT](../../../src/test/java/com/financeclaw/m2/GovernanceBoundariesIT.java) 的 7 个真实 PostgreSQL 场景：授权回执、Artifact scope、报告效果完成写入后抛错并比较六张表；三个用例加入外层事务回滚；旧 grant/effect 回执重放；不确定效果阻断归档与发布。这 7 项本轮只完成编译，尚未执行。

Docker Engine 只读检查仍返回 HTTP 500，未重启或修改 Docker。数据库、角色启动、独立进程恢复及 Compose 验证均待环境恢复；之前 R6 的 835 项完整回归和 27 项 Compose 结果仍是历史基线。

恢复后执行：

```bash
scripts/mvn.sh -B -ntp clean verify spotless:check
python3 scripts/validate-compose.py --ziwei --admission --preflight --restore-privacy
python3 scripts/check-delivery.py
```

本批完成了原 GovernanceService 的职责拆分；上述编译范围与 Docker 故障保留为该批历史证据。2026-10-21 已补跑[完整集成验证](post-refactor-verification.md)，修复三个故障注入夹具错误后，7 项 GovernanceBoundariesIT 全部通过，另完成角色/恢复与 27 项 Compose 检查。真实供应商、飞书租户、生产身份与灾备继续按[迁移验收清单](../acceptance-checklist.md)处理。Bean Validation/类型化 DTO 已进入后续[HTTP 请求校验切片](../../architecture/http-request-validation.md)，[R7a 入口探针](r7a-subagent-evaluation.md)单独记录。
