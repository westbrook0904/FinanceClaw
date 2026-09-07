# FinanceClaw 包结构与依赖规则

FinanceClaw 采用面向业务模块的模块化单体，外围按协议、应用用例、Agent 编排和基础设施划分。
当前领域包内同时包含模型、规则与持久化实现，属于务实的模块内聚设计，并非所有依赖都通过 Port
倒置的纯领域架构。本文描述现状及新增代码应遵循的边界；具体问题和调整顺序见
[2026-09-07 分包复查](package-review-2026-09-07.md)。

`.redesign/02-目标模块与依赖设计.md` 中的早期扁平目录已被本布局替代；领域职责与原生框架优先
的原则仍有效，不应据该早期目录重新创建旧包。

## 顶层包职责

| 包 | 职责 | 当前依赖边界 |
|---|---|---|
| `kernel` | 身份、执行上下文、Target、API 共享契约 | Pydantic 与标准库 |
| `modules` | 会话、执行、交互、委派、记忆、制品、审计、Outbox、Workflow、紫微 | `kernel`、SQLAlchemy 等模块所需库、共享 ORM、明确的模块协作 |
| `application` | 跨模块用例、提交/恢复协调和出站 Port | `kernel`、`modules`、Agent/Tool 目录；仍有待移出的中间件工具函数依赖 |
| `orchestration` | ReAct Agent、LangGraph、Tool 治理与工作流 Graph | `kernel`、`modules`、应用用例、LLM 配置/工厂；graph 入口另做装配 |
| `infrastructure` | 配置、数据库、迁移、LLM、Agent Server 客户端、观测和安全适配 | 上层定义的 Port、领域模型/表与第三方 SDK |
| `interfaces` | HTTP/SSE 与飞书 WebSocket 的协议适配、输入规范化和生命周期 | `application`、公开契约/异常；HTTP 装配函数还依赖具体基础设施 |
| `operations` | 运维 smoke、在线探针和评测数据命令 | 正式公开的应用/基础设施接口 |
| `evaluation` | 离线回归集、评分和发布门禁 | 稳定数据契约与 LangSmith SDK |

## 领域模块职责

| 模块 | 拥有的事实或规则 | 与邻近模块的界限 |
|---|---|---|
| `conversation` | 永久会话/Turn/消息 Journal、摘要、上下文选择与 Manifest | Journal 是历史依据，checkpoint 是运行恢复状态；两者不互相替代 |
| `execution` | 授权与发布快照、start/resume 操作日志、根任务预算、取消事实 | 记录提交与观察事实；节点执行、队列和 checkpoint 仍由 Agent Server 管理 |
| `interactions` | 发布交互点、待回答实例、唯一用户决定 | 决定落盘不等于远程恢复完成；实际提交交给应用层执行服务 |
| `delegation` | 父子运行关联、目标版本、输入摘要、子执行与结果交付状态 | 子运行独占 thread，父 Agent 等待结构化结果；不能把已交付当作执行成功 |
| `workflows` | 发布定义、运行记录、固定审批单 | 业务流程节点位于 `orchestration/graphs/workflows`，目录和运行事实留在模块内 |
| `memory` | 长期记忆类型、证据、写入/召回策略和生命周期 | 当前直接使用 LangGraph Store；与永久会话原文和短期 checkpoint 分开 |
| `artifacts` | 大结果元数据、归属校验、内容完整性与存储服务 | 存储后端当前位于本模块；元数据表暂留在 conversation，是待纠正的归属偏差 |
| `audit` | 永久审计事件与追加记录 | 同事务追加 Outbox；普通日志、trace 和事件投递结果不替代审计事实 |
| `outbox` | 待投递事件、租约、重试、死信 | 只负责可靠外发，不承担业务图调度 |
| `ziwei` | 出生资料规范化、固定规则、确定性盘面及解读契约 | 引擎经 Port 注入；制品持久化归 application，模型取证/解读归 orchestration |

## 新增代码的依赖原则

1. `application` 拥有 `AgentServerClient` 等出站 Port，基础设施只负责实现，不反向定义业务接口。
2. HTTP 与 Channel 层只完成协议适配，不复制 Conversation、Workflow 或 Delegation 业务规则。
3. `bootstrap.py` 集中选择组件实现；`interfaces/http/app.py:create_default_app` 装配 HTTP 客户端、
   认证、服务与生命周期；`orchestration/graphs/server_graphs.py` 装配并注册服务端图。具体实现的
   选择应集中在这些装配入口，业务代码不得导入会在导入时启动资源的 graph 注册模块。
4. 模块间优先通过稳定模型与公开服务协作。现有跨表事务包括 Audit/Outbox、交互决定/Workflow
   审批/执行操作、委派交付/执行观察；这些耦合必须明确记录，不能以“同一个数据库”为由任意跨表写入。
5. 运维命令不放入 `application`，避免生产用例包混入可执行脚本和环境探针。
6. 新模块必须说明职责与依赖；类和函数注释解释意图、状态含义、归属、单位和事务约束，避免复述代码。
   Pydantic 模型、Tool 输入和结构化输出的类 docstring 可能进入 JSON Schema 或模型提示，因此纯可读性
   补充优先使用普通 `#` 注释。修改 Schema 描述也应当作为契约变更审查。
7. `kernel` 仅保存真正跨领域的稳定契约，不能成为通用工具函数和领域 DTO 的堆放处。现有 API 请求/响应
   与核心身份混放在此属于历史折中，后续按调用方迁移，禁止继续扩大。

## 阅读入口

- 产品会话：`interfaces/http/app.py` → `application/conversation_service.py` →
  `application/execution_service.py` → `application/ports/agent_server.py`。
- 用户回答：`application/interaction_service.py` 校验发布与权限 →
  `modules/interactions/repository.py` 同事务保存决定和命令 → `ExecutionService` 提交恢复。
- Agent 执行：`orchestration/graphs/server_graphs.py` → `agents/factory.py` → 各 Middleware 和 Tool。
- 紫微领域：`orchestration/graphs/ziwei_agent.py` → `application/ziwei_service.py` →
  `modules/ziwei/service.py` → `modules/ziwei/ports.py`，具体引擎在 `infrastructure/ziwei`。

包级 `__init__.py` 目前存在聚合导出，导入一个目录类型可能同时加载工厂、中间件和仓储。阅读或
判断依赖时需要追踪这些导出，不能只根据调用点的 `from ... import ...` 判断依赖是否轻量。

## 兼容性策略

此前包布局迁移已同步更新仓内调用方，不保留旧包路径的转发壳。旧路径如果继续保留，会掩盖错误依赖并让
新代码继续引用废弃边界。外部调用方应直接迁移到本文列出的正式包路径；本次注释复查未再次迁移包路径。
