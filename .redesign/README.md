# FinanceClaw 当前架构

Stage 10 的统一 AgentServer API 与 Turn/Command/Interaction 继续保留。Stage 11 新增独立 memory_worker，形成 api、worker、integrations、memory_worker 四种角色；18 张应用表使用唯一初始迁移。SQL 管理长期记忆与画像事实，Store 管理派生检索索引，checkpoint 管理工作上下文。

- [架构基线](00-最终架构设计.md)
- [架构决议](01-架构决议汇总.md)
- [模块与依赖](02-目标模块与依赖设计.md)
- [持久化设计](03-数据模型与持久化设计.md)
- [安全、观测与评测](04-安全观测与评测设计.md)
- [产品 API](05-顶层Agent与对外接口修订.md)
- [Stage 8 Hotfix 实施方案](stages/stage-8-hotfix-实施方案.md)
- [Stage 9：上下文与记忆优化实施方案](stages/stage-9-上下文与记忆优化实施方案.md)
- [Stage 9 实现与验证](stages/stage-9-实现与验证.md)
- [Stage 10：统一 API 与运行模型收敛实施方案](stages/stage-10-统一API与运行模型收敛实施方案.md)
- [Stage 10 实现与验证](stages/stage-10-实现与验证.md)
- [Stage 11：异步记忆与上下文治理实施方案](stages/stage-11-异步记忆与上下文治理实施方案.md)
- [Stage 11 场景链路与验收矩阵](stages/stage-11-场景链路与验收矩阵.md)
- [Stage 11 实现与验证](stages/stage-11-实现与验证.md)
- [上下文与记忆评估依据](../docs/architecture/memory-assessment-2026-09-10.md)
- [清理与验证](stages/stage-8-hotfix-清理与验证.md)
- [Stage 6：Feishu P2P Channel](./stages/Stage-6-Feishu-P2P-Channel-实施说明.md)
  - [Stage 6 验证记录](./stages/Stage-6-验证记录.md)
- [飞书交互卡片适配实施方案](stages/Feishu-交互卡片适配实施方案.md)
- [紫微候选](stages/Stage-7-Ziwei-Domain-Agent-设计说明.md)

已废弃的部署、跨运行协议和迁移资料从工作树删除，历史通过 Git 查看。
