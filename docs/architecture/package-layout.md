# FinanceClaw 包结构与依赖规则

FinanceClaw 采用面向业务模块的模块化单体，并用清晰的边界层控制依赖方向。业务能力按领域聚合，
不会为了追求目录形式把一个用例拆散到大量无业务含义的横向包中。

## 顶层包职责

| 包 | 职责 | 允许依赖 |
|---|---|---|
| `kernel` | 身份、执行上下文、Target、API 共享契约 | Pydantic 与标准库 |
| `modules` | Conversation、Memory、Workflow、Delegation、Artifact、Audit、Outbox 业务模块 | `kernel`、模块内代码、共享 ORM 基类 |
| `application` | 跨模块用例、事务流程和出站 Port | `kernel`、`modules`、`orchestration` 契约、Port 抽象 |
| `orchestration` | ReAct Agent、LangGraph、Tool 治理与工作流 Graph | `application`、`kernel`、`modules` |
| `infrastructure` | 配置、数据库、迁移、LLM、Agent Server 客户端、观测和安全适配 | 上层定义的 Port 与第三方 SDK |
| `interfaces` | HTTP/SSE 与飞书 WebSocket 的协议适配、输入规范化和生命周期 | `application` 与组合根 |
| `operations` | 运维 smoke、在线探针和评测数据命令 | 正式公开的应用/基础设施接口 |
| `evaluation` | 离线回归集、评分和发布门禁 | 稳定数据契约与 LangSmith SDK |

## 依赖原则

1. `application` 拥有 `AgentServerClient` 等出站 Port，基础设施只负责实现，不反向定义业务接口。
2. HTTP 与 Channel 层只完成协议适配，不复制 Conversation、Workflow 或 Delegation 业务规则。
3. `bootstrap.py` 是唯一组合根；只有它可以同时了解业务模块与具体基础设施实现。
4. 业务模块之间通过稳定模型协作；跨模块 ORM Table 引用只允许用于明确的外键或原子 Outbox 写入。
5. 运维命令不放入 `application`，避免生产用例包混入可执行脚本和环境探针。
6. 新模块必须拥有模块级说明；公开类和函数必须有说明意图的 docstring，复杂函数还要标注主要步骤。

## 兼容性策略

本次重构同步更新所有仓内调用方，不保留旧包路径的转发壳。旧路径如果继续保留，会掩盖错误依赖并让
新代码继续引用废弃边界。外部调用方应直接迁移到本文列出的正式包路径。
