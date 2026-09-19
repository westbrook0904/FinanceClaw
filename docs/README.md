# FinanceClaw 文档导航

这里是当前代码的使用与维护入口。项目展示和三个场景演示见[项目主页](../README.md)；第一次运行从[本地完整链路](operations/local-full-stack.md)开始，准备参与开发则阅读[开发上手](development.md)。

FinanceClaw 通过飞书单聊或 HTTP API 接收任务，根 Agent 按需调用子 Agent、MCP 工具和 Skills。当前部署有 API、图执行 Worker、异步记忆 Worker、Integrations 四种角色；业务 API 与 LangGraph AgentServer 共用一个 ASGI 进程。

## 按目标选择阅读路径

| 你的目标 | 建议顺序 | 完成后应能做什么 |
| --- | --- | --- |
| 在本机跑通一个任务 | [本地完整链路](operations/local-full-stack.md) → [Turn 接口](operations/turn-control.md) | 确认服务就绪、提交任务、读取结果，区分受理与完成 |
| 体验飞书与三个演示场景 | [本地完整链路](operations/local-full-stack.md) → [紫微](operations/ziwei-agent.md) / [酒店 MCP](operations/mcp.md) / [Skills](operations/skills.md) | 配置所需身份与凭据，使用进度卡片和任务交互 |
| 理解实现并修改代码 | [开发上手](development.md) → [包结构](architecture/package-layout.md) → [请求调用链](architecture/request-lifecycle.md) | 从入口找到状态流转、图装配和扩展实现 |
| 调整模型或控制上下文 | [模型配置](operations/model-configuration.md) → [上下文与记忆](operations/context-budget.md) | 找到模型别名、容量、调用预算与压缩策略 |
| 排查任务或数据问题 | [Turn 控制](operations/turn-control.md) → [通知](operations/notifications.md) / [记忆后台任务](operations/memory-outbox.md) | 按任务状态和进程职责定位问题，选择正确的恢复入口 |
| 检查变更或准备交付 | [测试指南](../tests/README.md) → [发布检查](operations/release-checklist.md) | 区分静态检查、自动化回归、数据库探针和真实服务验收 |

## 文档目录

### 上手与架构

- [开发上手与改动导航](development.md)：解释器、最小开发循环、配置归属和改动入口。
- [包结构与依赖边界](architecture/package-layout.md)：四种运行角色、目录职责和数据归属。
- [一次请求如何完成](architecture/request-lifecycle.md)：从任务受理到模型执行、人工恢复和结果提交的源码路径。
- [环境配置](../config/environments/README.md)：应用与记忆 Worker 的配置文件、共享策略和专用凭据。

### 能力配置与使用

- [本地完整链路](operations/local-full-stack.md)：Compose 启动、健康检查和飞书接入。
- [模型配置](operations/model-configuration.md)：供应商、模型别名、Agent 与后台任务覆盖。
- [MCP 接入](operations/mcp.md)：固定工具定义、RollingGo 酒店查询及大型结果读取。
- [Skills](operations/skills.md)：技能表单、命令、按需加载与固定包发布。
- [紫微 Subagent](operations/ziwei-agent.md)：开发候选的启用、输入、澄清和领域边界。
- [Taibu MCP](operations/taibu-mcp.md)：可选黄历与八字服务，独立于紫微领域子图。

### 运行与数据维护

- [Turn 控制](operations/turn-control.md)：任务接口、状态、取消、授权与人工交互。
- [通知投递](operations/notifications.md)：飞书进度卡片、投递重试与不确定回执。
- [上下文与记忆](operations/context-budget.md)：上下文容量、记忆权限和检索投影。
- [记忆后台任务](operations/memory-outbox.md)：异步提取、整合、索引及受控重放。
- [数据主体请求](operations/data-subject-requests.md)：查询、纠正、遗忘与保留策略。
- [生产运维](operations/production-runbook.md)、[发布检查](operations/release-checklist.md)、[故障恢复](operations/disaster-recovery.md)：部署要求、角色健康与恢复边界。

### 测试与设计依据

- [测试指南](../tests/README.md)：测试目录、运行条件、CI 和外部验证边界。
- [持久化与恢复探针](../experiments/stage10/README.md)：隔离环境中的真实协议和数据库验证。
- [设计与验证记录索引](../.redesign/README.md)：按阶段保存的设计、实施记录和证据。
- [2026-09-10 记忆评估](architecture/memory-assessment-2026-09-10.md)：特定时间点的架构评估，阅读前先看文首适用范围。

## 阅读时先分清三件事

**现行手册与历史记录。** `docs/operations/`、架构说明和配置 README 描述当前实现；`.redesign/stages/` 和实验记录保存当时的设计、命令与验证结果。历史版本号、测试数量和本机状态不能直接当作当前状态使用。

**离线协议验证与真实使用效果。** 确定性离线模型用于跑通任务和协议。真实模型的回答质量、外部 MCP 的账户权限、飞书控件与回调需要对应服务的单独验证。紫微目前仍限定在开发 / 测试候选范围，行情样例使用演示数据。

**普通文档与运行时技能。** `financeclaw/shared/skills/builtin/` 中的 `SKILL.md` 和资源会作为模型输入，受固定版本、包 hash 和来源许可约束。修改这些文件属于能力发布变更，应按 [Skills 发布流程](operations/skills.md)处理。

## 维护规则

- 命令默认在仓库根目录运行；依赖以 `pyproject.toml`、`uv.lock` 和 CI 为准。
- 配置默认值以示例环境文件、`config/models.toml`、`config/mcp.toml` 与 Settings 实现共同核对，文档示例不代表已经提供凭据。
- 修改接口、配置或运行角色时同步更新对应手册；跨模块调用变化同步更新架构与请求链路。
- 验证记录注明日期、运行条件和结果范围。设计完成、测试通过、模拟集成和真实环境验收分别记录。
