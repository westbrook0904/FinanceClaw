# 架构演进与验证档案

这里保存 FinanceClaw 的架构决议、阶段实施方案及当时的验证证据，用于理解“为什么这样设计”和复现某次改动。当前上手与运维入口是 [`docs/`](../docs/README.md)；不要直接把历史方案中的接口、目录、配置或部署命令用于当前版本。

阶段文件中出现的“当前”“已完成”、版本号和通过数量均以该文件记录的日期、代码及验证条件为范围。后续实现可能已经替换当时方案；阅读顺序是当前运行文档 → 相关源码/测试 → 历史设计与证据。本文只整理导航，不重写历史结论。

## 先找到当前入口

| 目的 | 当前文档 |
|---|---|
| 了解项目和演示能力 | [项目主页](../README.md) |
| 按任务选择文档 | [文档导航](../docs/README.md) |
| 理解角色、模块与依赖 | [代码结构](../docs/architecture/package-layout.md) |
| 本机启动与排障 | [本地完整栈](../docs/operations/local-full-stack.md) |
| 理解任务受理、恢复和取消 | [Turn 运行控制](../docs/operations/turn-control.md) |
| 上下文与长期记忆 | [上下文预算](../docs/operations/context-budget.md)、[异步记忆运维](../docs/operations/memory-outbox.md) |
| 确定测试范围和运行条件 | [测试与验证指南](../tests/README.md) |

## 架构决议快照

这些编号文件保留了重构期间的设计汇总，部分已在后续阶段被修订。文件名中的“最终”表示当时的决议，不表示后续能力都已经包含在内。

| 文件 | 阅读重点 |
|---|---|
| [00 · 架构汇总](00-最终架构设计.md) | Stage 11 角色与包边界快照 |
| [01 · 架构决议](01-架构决议汇总.md) | 关键取舍与约束 |
| [02 · 模块与依赖](02-目标模块与依赖设计.md) | 当时的模块职责分工 |
| [03 · 数据模型](03-数据模型与持久化设计.md) | 应用事实、原生状态与检索投影的分工 |
| [04 · 安全、观测与评测](04-安全观测与评测设计.md) | 身份、权限、数据出口、审计和评测设计 |
| [05 · 产品 API](05-顶层Agent与对外接口修订.md) | 顶层 Agent 与对外接口的设计演进 |

## 阶段实施与验证

| 主题 | 设计或实施说明 | 验证记录与证据 |
|---|---|---|
| 飞书 P2P 接入 | [Stage 6](stages/Stage-6-Feishu-P2P-Channel-实施说明.md) | [验证记录](stages/Stage-6-验证记录.md) |
| 紫微领域 Agent | [Stage 7](stages/Stage-7-Ziwei-Domain-Agent-设计说明.md) | 当前回归与边界见 [紫微运行说明](../docs/operations/ziwei-agent.md) |
| BFF 与内部子图 | [Stage 8 Hotfix](stages/stage-8-hotfix-实施方案.md) | [清理与验证](stages/stage-8-hotfix-清理与验证.md)、[原始证据](evidence/stage8-hotfix/) |
| 原生上下文与记忆 | [Stage 9](stages/stage-9-上下文与记忆优化实施方案.md) | [实现与验证](stages/stage-9-实现与验证.md)、[原始证据](evidence/stage9/) |
| 统一 API 与运行模型 | [Stage 10](stages/stage-10-统一API与运行模型收敛实施方案.md) | [实现与验证](stages/stage-10-实现与验证.md)、[原始证据](evidence/stage10/) |
| 异步记忆与上下文治理 | [Stage 11](stages/stage-11-异步记忆与上下文治理实施方案.md)、[场景与验收矩阵](stages/stage-11-场景链路与验收矩阵.md) | [实现与验证](stages/stage-11-实现与验证.md)、[原始证据](evidence/stage11/) |

Stage 9 之前的问题分析另见 [2026-09-10 记忆与上下文评估](../docs/architecture/memory-assessment-2026-09-10.md)。其中旧 Journal 拼装、摘要和 Store 结论属于评估时点，后续 Stage 9/11 的实现与运行文档应一起阅读。

## 能力扩展记录

| 主题 | 历史设计与结果 | 当前操作入口 |
|---|---|---|
| 飞书原生交互卡片 | [适配方案](stages/Feishu-交互卡片适配实施方案.md) | [通知与飞书](../docs/operations/notifications.md) |
| 太卜黄历与八字 MCP | [接入方案](stages/taibu-mcp-接入实施方案.md)、[联调证据](evidence/taibu-mcp/) | [太卜运行说明](../docs/operations/taibu-mcp.md) |
| Skills、调酒技能及技能表单 | [运行时方案](stages/skills-运行时接入实施方案.md)、[实现与验证](stages/skills-实现与验证.md) | [Skills 运行说明](../docs/operations/skills.md) |
| 通用 MCP 与 RollingGo | [配置化接入方案](stages/MCP-工具主动发现与配置化接入实施方案.md) | [MCP 运行说明](../docs/operations/mcp.md) |
| MCP 大结果与工件读取 | [实施方案及结果](stages/MCP-大结果归档与结构化读取实施方案.md)、[证据](evidence/mcp-views/) | [MCP 运行说明](../docs/operations/mcp.md)、[上下文预算](../docs/operations/context-budget.md) |

## 如何读验证证据

- **设计或静态检查**说明计划、接口或结构符合检查条件，不说明服务已经部署。
- **自动化回归**证明用例覆盖的行为；模型、HTTP、数据库或渠道可能使用替身，应看具体条件。
- **真实数据库/HTTP 探针**说明对应服务链路在当次环境下可用，合成模型或合成数据仍不证明实际回答质量。
- **真实模型与飞书验收**需要明确模型、输入范围、客户端操作和观察结果；连接成功、API 返回成功或离线图通过不能互相替代。

`evidence/` 保存当时输出，复现脚本位于 [`experiments/`](../experiments/)。重新运行前先检查依赖、隔离环境、凭据和清理范围。新增结果应明确日期、代码版本、执行条件和未验证项；保留旧证据的时间归属，不把旧成功报告直接当成当前交付结果。

已从工作树删除的旧部署或跨运行协议资料通过 Git 历史查看。新功能的日常操作说明维护在 `docs/`，设计取舍与一次性实施证据归档在这里。
