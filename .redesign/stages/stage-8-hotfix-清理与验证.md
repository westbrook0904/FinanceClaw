# Stage 8 Hotfix 清理与验证

日期：2026-09-09。状态：清理完成。代码以当前架构为唯一基线，项目尚未上线，不保留旧运行体系的兼容路径。

## 清理结果

- 删除独立协调服务包及其 Ingress／Worker／运维命令、容器配置和环境字段。
- 删除跨运行委托协议、子运行创建／接管／历史迁移、回调别名、旧驱动选择和旧发布目录。
- 删除独立 Tool／Workflow／Agent target 的创建与通用 resume API、对应图导出和旧冒烟脚本。
- 只注册 `finance_agent_v1_5_0` 根图。领域 Agent 与 Workflow 通过 `call_agent__*`／`call_workflow__*` 在根 ReAct 中调用。
- 业务表使用 `root_runs`、`run_inbox`、`run_control`；保留 Journal、授权、原生 attempt、交互、审计和通知事实。数据库约束要求执行记录 `root_run_id = run_id`。
- 未发布迁移合并为 `0001_initial`，共 22 张业务表。空库初始化默认关闭新受理，使用 BFF 控制命令显式开放。
- 删除描述旧架构的方案、部署步骤和证据，历史通过 Git 查看；当前架构、API 和运维文档同步更新。

BFF 负责 start、人工 resume、cancel 与结果收尾，Webhook 接收器和周期核对随 BFF 运行。客户端断线后仍可完成并写入聊天记录。通知发送器继续作为 BFF 包内的独立进程，使用共享业务库。

## 验证结果

| 验证 | 结果 |
|---|---|
| 待提交文件快照全量 pytest | 245 passed、5 skipped，55.94 秒 |
| Ruff 检查与格式检查 | 通过 |
| 凭据扫描、Git diff 空白检查 | 通过 |
| 从干净快照构建 wheel | 通过；无旧服务／委托路径，仅包含初始迁移 |
| 空 SQLite 库迁移 | 与运行时 metadata 一致；回滚、再次升级通过 |
| 数据库执行边界与部署 CAS | 子运行写入被拒绝；同 revision 并发配置只有一个成功 |
| HTTP 入口与只读观察 | 旧创建／resume 路由返回 404；message 中外加 Target 返回 422；SSE 独立游标和通知归属通过 |
| 原生混合子图 HTTP | 一个业务根、一个原生 thread；四次人工恢复，最终仅一条 user 和一条 assistant Journal |
| 原生 HITL HTTP | 一个业务根、一个原生 thread；两次人工恢复，批准／拒绝与最终 Journal 通过 |

两个原生场景均启动隔离 BFF 和 LangGraph dev，使用合成模型与临时业务库，验证 BFF 重启后恢复、重复 Webhook、无客户端连接时收尾，以及停止新受理后已有根继续恢复。原生 Run 数分别为 5 和 3，来自初始 start 加人工 resume；Worker 没有额外业务运行或 HTTP thread。

通知 SDK、SSE 重连和共享 Session 回滚测试保留并适配当前 BFF，领域 Worker 测试保留真实发布、权限、预算和原生图校验。待提交快照不包含原有本地完整部署文件及相关未提交改动。

5 项跳过涉及专用 PostgreSQL 并发环境与外部 Provider／飞书凭据。两条 warning 来自飞书 SDK 的弃用 API。本次不声称验证了生产 Agent Server 的持久队列崩溃恢复或真实飞书投递。

没有修改本机已有数据库。开发部署请使用新空业务库执行初始迁移；初始化和运行控制见 [BFF 手册](../../docs/operations/bff-run-control.md)。

验证记录：[汇总](../evidence/stage8-hotfix/cleanup-verification.json)、[原生混合子图](../evidence/stage8-hotfix/cleanup-native.json)、[原生 HITL](../evidence/stage8-hotfix/cleanup-hitl-native.json)。原生报告包含参与验证的源码 SHA-256。
