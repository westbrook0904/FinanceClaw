# Stage-8 前置分包：实施与验证

日期：2026-09-08。实现边界见[包结构设计](package-layout.md)。本轮完成职责分包，
Stage-8 的独立 Coordinator Service、Webhook、持续 Worker 与通知可靠交付继续按方案实施。

## 实施结果

| 内容 | 结果 |
|---|---|
| 三个职责包 | `bff`、`coordination`、`agent_server`，分别提供 bootstrap |
| 会话职责 | BFF 创建/读取 Journal；Coordination 管理 Turn 提交、恢复、取消与父子推进 |
| 发布与执行 | `kernel` 保存类型/Schema，`shared/releases` 保存唯一声明；Tool/Graph 实例只在执行端构造 |
| 同库装配 | BFF 与 Coordination 复用应用数据库和 Session 工厂，保持一个 Alembic 迁移序列 |
| 共享事实 | Journal、执行账本、制品、审计、Outbox 各保留一份；既有跨表事务保持原子性 |
| 表归属 | `ArtifactMetadataRow` 移至 `shared/artifacts/tables.py`，其他运行事实映射归共享执行账本 |
| 导入与入口 | 移除旧包及根 bootstrap，无转发壳；同步主入口、两份 LangGraph 配置、脚本、测试和打包资源 |
| 依赖防线 | 检查目录真实存在、绝对/相对导入、聚合导出、冷导入和角色装配 |

代码修改前已备份工作区并记录测试基线；既有本地部署文件和配置编辑保留。
`settings.py` 工作区内容与开工时逐字节相同；提交只迁移原有版本，连接超时上限的本地调整继续留在工作区。

## 验证结果

| 检查 | 结果 |
|---|---|
| 开工基线 `pytest -q -m 'not external'` | 226 passed、7 skipped、2 deselected |
| 完整离线回归，同一命令 | **254 passed、7 skipped、2 deselected** |
| 推送前独立导出的待提交快照，同一命令 | **250 passed、7 skipped、2 deselected**；不含原有未跟踪飞书测试的 4 项 |
| Ruff check / format check | 通过，覆盖生产代码、脚本、测试和主入口 |
| 凭据扫描、`git diff --check` | 通过 |
| 独立进程导入/装配 | BFF 和 Coordination 不加载 AgentServer；不导入 graph 注册入口或启动飞书 SDK |
| Ziwei 开关两种配置 | BFF 仅装配发布声明；协调端与执行端的 Agent/Tool/Workflow 发布契约一致 |
| 迁移前后快照比较 | Agent 配置/指纹、Tool Schema/描述、Workflow Schema/发布元数据完全一致 |
| PostgreSQL DDL 比较 | 15 张表及索引定义完全一致；迁移头仍为 `0008_stage6fix_c` |
| 分发包检查 | 构建 sdist 与 wheel；wheel 含三包、`py.typed`、迁移模板及 8 个 revision，无废弃包 |
| wheel 脱离源码目录导入 | 三个角色入口及迁移资源读取通过 |

新增检查位于 `tests/architecture/test_service_bootstrap.py` 和
`tests/stage5/test_package_architecture.py`。既有故障、委派、交互、审批、预算、飞书和紫微回归继续运行；
跨角色测试由 `tests/support.py` 显式组合执行端与协调端仓储，生产装配不依赖该测试夹具。

本轮未运行需真实凭据/服务的 external 测试，没有对实际部署做滚动升级验收。
原有 `status()` 和 SSE 的执行推进语义继续保留，不能把本次分包视为后台协调功能已完成。
