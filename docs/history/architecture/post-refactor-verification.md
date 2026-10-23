# 职责拆分与 HTTP 校验的完整集成验收

> **历史记录**：本文保留当时的设计、命令与验证结果，不代表当前独立应用的部署结构或最新测试结论。当前操作见[运行说明](../../operations.md)，新库结构见[初始化说明](../../architecture/fresh-schema.md)；历史类名、版本号和外部导出命令仅用于理解证据。

验证日期：2026-10-21。本地检查点 `431c07d` 保存了任务、受理和记忆接口的 HTTP 校验改动，未推送；其前序 `4f4f7b4` 保存原生执行与治理用例拆分。

此前数批验证因 Docker API 返回 HTTP 500，只完成单元、HTTP、架构和产品 Jar 检查。Docker Engine 恢复后，本批补跑数据库、真实角色装配、独立进程恢复及隔离 Compose 演练。历史证据文件保留当时的验证范围，后续结果以本文为准。

## 验证入口

```bash
# 全部单元、HTTP、架构、PostgreSQL 与独立进程测试
scripts/mvn.sh -B -ntp clean verify spotless:check

# 修复治理故障注入夹具后，重跑该集成类及新增的 R7a 探针
scripts/mvn.sh -B -ntp spotless:apply test -Dtest=SubAgentToolCompatibilityTest \
  failsafe:integration-test failsafe:verify -Dit.test=GovernanceBoundariesIT spotless:check

# 使用本地开发 secrets，创建随机隔离项目，离线模型和合成行情
python3 scripts/validate-compose.py --ziwei --admission --preflight --restore-privacy

python3 scripts/check-delivery.py
```

Compose 脚本不读取 `.env` 中的供应商配置，并清空继承的 `FINANCECLAW_*` 环境变量。只删除本次随机项目创建的容器和卷；不会清理其他开发实例。凭据内容不进入验收记录。

## 验证范围

- `GovernanceBoundariesIT`：授权回执、Artifact scope、报告效果写入后故障与外层事务回滚；旧回执重放和不确定效果阻断。
- `RoleAssemblyIT`：API、Worker、Integrations、Standalone 及独立 Memory/Migrate 的实际 Bean、Mapper、HTTP 和数据库身份。
- 任务、交互、记忆、市场研究和紫微的既有 PostgreSQL 与独立进程测试：保留租约、取消、状态 CAS、隐私核对与恢复行为。
- 隔离 Compose：部署启动、权限、暂停与排空、进程和数据库故障、备份恢复，以及旧备份缺少后续遗忘记录时的隔离。

上述测试使用离线模型、模拟供应商或合成输入，不能替代真实模型、远端 MCP、飞书租户或生产迁移验收。

## 本轮结果

首次完整命令执行了 906 项单元/HTTP/架构测试和 215 项集成测试。前者全部通过；后者 212 项通过，`GovernanceBoundariesIT` 的三项故障注入在设置 Mockito spy 时出错，因此首次命令为 **BUILD FAILURE**，不能记录为一次全绿构建。

原因是直接对 Spring 的 Repository 代理做 spy，Mockito 设置 `doAnswer` 时，Spring MockResolver 解包代理再次进入 Mockito，触发未完成 stubbing。修复改为先用 Spring `AopTestUtils.getUltimateTargetObject` 取得真实 gateway，再进行故障注入。三个 gateway 本身没有事务注解；其 Mapper 仍是容器注入的 MyBatis 代理，应用服务的真实 `TransactionTemplate` 与 `leases.fenced` 保持不变。测试依然执行真实 SQL 后抛错，并比较六张业务表的完整行快照；没有以 mock 成功替代数据库回滚。

修复后定向运行 `GovernanceBoundariesIT` 的 7 项测试全部通过，包含上述三项真实回滚；新增 R7a 的 6 项探针全部通过，Spotless 通过，命令为 **BUILD SUCCESS**。首次报告在重跑前已独立保存；完整运行与定向修复共同覆盖 906 项既有单元/HTTP/架构、215 项集成测试，另有 6 项新增 SDK 探针。没有在修复后再次执行整个 `clean verify`，也不把混合生成时间的 XML 当作一次完整成功运行。

已核对首次运行的 332 个构建输入：修复仅改变 `GovernanceBoundariesIT`，另新增一个 R7a 测试文件；生产源码、资源、依赖与迁移不变，定向重跑前后的产品 Jar SHA-256 相同。

隔离 Compose **27 项检查全部通过**，包括四角色启动与权限、持久暂停/排空、SIGKILL 后审批恢复、数据库中断、全表备份恢复、旧备份缺失后续遗忘的拦截，以及恢复后继续接收新任务。其 Jar SHA-256 与首次完整运行的产品包相同。随机项目 `financeclaw-m5e-41fb87a563` 的容器和卷已全部清理；Docker Engine 版本为 26.1.1。

完整首次结果、修复重跑、Compose 原始检查、源码和报告摘要见[机器证据](../../architecture/evidence/post-refactor-verification.json)。本批测试修复与记录随 `codex/java-agentscope` 分支交付；此前两批 HTTP 校验已由 `431c07d` 保存。机器证据保留采集时的提交状态，不覆盖历史记录。真实供应商、远端 MCP、飞书租户和生产切换仍需独立验收。
