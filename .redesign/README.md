# FinanceClaw 当前架构

BFF 负责产品入口、根运行控制与 Journal；Agent Server 执行顶层 ReAct 和内部 Worker。业务库共享，原生执行持久化由 Agent Server 管理。项目尚未正式上线，当前数据库从初始 schema 创建。

- [架构基线](00-最终架构设计.md)
- [架构决议](01-架构决议汇总.md)
- [模块与依赖](02-目标模块与依赖设计.md)
- [持久化设计](03-数据模型与持久化设计.md)
- [安全、观测与评测](04-安全观测与评测设计.md)
- [产品 API](05-顶层Agent与对外接口修订.md)
- [Stage 8 Hotfix 实施方案](stages/stage-8-hotfix-实施方案.md)
- [清理与验证](stages/stage-8-hotfix-清理与验证.md)
- [Stage 6：Feishu P2P Channel](./stages/Stage-6-Feishu-P2P-Channel-实施说明.md)
  - [Stage 6 验证记录](./stages/Stage-6-验证记录.md)
- [飞书交互卡片适配实施方案](stages/Feishu-交互卡片适配实施方案.md)
- [紫微候选](stages/Stage-7-Ziwei-Domain-Agent-设计说明.md)

已废弃的部署、跨运行协议和迁移资料从工作树删除，历史通过 Git 查看。
