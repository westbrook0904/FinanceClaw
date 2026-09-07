# FinanceClaw 分包复查（2026-09-07）

## 结论与范围

现有“业务模块 + 外围分层”的方向合理，适合当前规模；不建议重新按技术类型把全部 service、model、
repository 拆成横向大包，也没有证据需要拆成微服务。优先修正实际依赖和职责归属，再考虑目录调整。

本次静态扫描覆盖 `financeclaw/` 的 152 个 Python 文件、291 个类。所有类和函数已有 docstring，
可读性问题主要是后续字段/职责未补充、简略说明和历史注释失真。注释复查阶段只修改注释及文档，
不迁移代码、不调整导入、不变更运行流程；用户随后要求修复回归失败，修复记录见文末。
下列结构调整均为后续建议，不代表已经实施。

## 合理之处

| 设计 | 代码依据 | 为什么保留 |
|---|---|---|
| 协议与用例分离 | `interfaces/http`、`interfaces/channels` 调用会话及交互服务 | HTTP 与飞书可以复用 Journal、授权和恢复流程 |
| 业务能力成组 | `modules/conversation`、`memory`、`workflows` 等 | 模型、规则及持久化按业务集中，修改一个能力时不必遍历大量横向目录 |
| 框架编排集中 | `orchestration/agents`、`graphs`、`tools` | Agent 中间件、图节点和工具适配有清晰阅读入口 |
| Agent Server 出站 Port | `application/ports/agent_server.py` 与 `infrastructure/clients/agent_server.py` | 应用协调提交/恢复，SDK 适配负责远程通信和回执 |
| 执行事实与交互事实独立 | `modules/execution`、`modules/interactions` | 授权快照、预算、用户决定和远程提交是不同生命周期，独立记录便于恢复与对账 |
| 紫微领域边界清楚 | `modules/ziwei/ports.py`、`application/ziwei_service.py`、`infrastructure/ziwei/x_iztro.py` | 计算不直接负责模型解读或存储，是新增领域能力可参考的拆分方式 |

## 需要收敛的边界

### 1. application 与 orchestration 存在双向依赖，应优先缩小

`ConversationService`、`DelegationService`、`TargetResolver` 通过 `orchestration.agents` 读取档案；
该包的 `__init__.py` 同时导出 `AgentFactory`、中间件和离线模型，因此“只引用目录”也会加载运行时实现。
`AgentProfile` 还从 `infrastructure.llm` 引用模型档案类型，契约和具体模型工厂的导出边界需要一起考虑。
`InteractionService` 以及 `ConversationService.resume` 还直接使用 `agents.middleware.redact_sensitive`。
反向上，紫微 Tool/Graph 调用应用层的 `ZiweiService` 和执行快照校验函数。

这证明包之间并非严格单向依赖，但不等于已经发生不可恢复的 Python 循环导入异常。局部导入能调整
加载时机，无法消除职责耦合。

建议先把纯脱敏函数放到双方都能依赖的独立模块，并分离目录契约与工厂导出；再评估将发布档案、
Schema 快照校验放入一个不依赖运行时的明确契约边界。只搬到 `agents/profiles.py` 还不够，Python
仍会先执行父包的 `__init__.py`。不应为满足单向箭头把这些内容全部塞进 kernel。

### 2. artifacts 的表归属与业务所有者不一致

`modules/artifacts/repository.py` 导入 `modules/conversation/tables.py` 的 `ArtifactMetadataRow`。
制品如今被工具、工作流和紫微结果使用，生命周期已超出会话模块，但修改其表仍需进入
conversation 包。这是最明确、最适合小步处理的归属偏差。

建议将 Python 表定义移动到 `modules/artifacts/tables.py`，同步数据库 metadata 注册和所有调用方。
表名、列、约束不变时不应为了目录搬迁制造数据库 DDL；仍需验证 Alembic metadata 与既有数据库兼容。
`LocalArtifactStore`/`S3ArtifactStore` 当前同置于 artifacts 的 `storage.py`，在模块化单体中可以接受；
只有准备统一所有存储适配时，才需要把具体后端进一步迁到 infrastructure。

### 3. execution 与 delegation 已有事务级双向耦合

`DelegationRepository` 创建 `ExecutionRepository`；后者的 `delivery_in_progress`、`observe` 又访问
`DelegationRow`，其中 `observe` 原子记录父恢复结果、交付标记及审计。这些写入有一致性理由，但已超出
通用“执行日志”职责，不能宣称 execution 是完全不理解业务的底层模块。

建议先把这些跨表原子操作作为明确的事务边界记录下来；待再次扩展交付流程时，由专门的交付协调服务
或事务接口承接委派专属更新。拆分必须保留现有原子性，不能简单改成几个分别提交的仓储调用。
`InteractionRepository` 同事务更新审批镜像、回答、出站操作及 Audit/Outbox，也应遵循相同原则。

### 4. Port 与实现的分离程度不一致

`ConversationService` 参数直接声明 `SqlAlchemyConversationRepository`；其他应用服务虽引用
`DelegationRepository`/`WorkflowRepository` 协议，却还使用实现上的 `.execution`。
`ExecutionRepository`、`InteractionRepository` 本身也是具体 SQLAlchemy 实现。
`memory/service.py` 直接使用 LangGraph Store，`conversation/context.py` 使用 LangChain 消息类型；
所以 `modules` 当前不是框架无关的纯领域层。

现阶段可以保留模块内仓储，避免仅为目录对称引入多余抽象；但对外协议应真实覆盖调用者所需能力。
后续如果需要替换后端或独立测试边界，优先补齐这些协议，并显式注入执行仓储，而不是继续从任意
repository 对象上取隐藏依赖。不要仅靠更换类型注解就声称已经完成依赖倒置。

### 5. 组合根分散是现状，应集中实现选择而非禁止入口装配

`bootstrap.py` 选择数据库、存储、工具及 Agent；`interfaces/http/app.py:create_default_app` 继续
创建 Agent Server 客户端、认证器和应用服务，并管理生命周期；`graphs/server_graphs.py` 加载组件、
创建模块级 graph。后者导入时会发生实际装配，应作为进程入口使用。

建议维持“公共组件装配 + 各进程入口装配”的分工；当 HTTP 文件继续增长时，将装配/lifespan 和
按资源组织的路由分离。当前文件约 938 行，其中包含较多注释，行数是阅读成本信号，不能单独作为
拆分依据。旧版文档中“只有 bootstrap 可以认识具体实现”的表述已在本次修正。

### 6. 应用服务需要按生命周期组织，不能按名称重复造抽象

Conversation、Workflow、Delegation 服务均超过千行（含注释），共有提交、观察、中断、恢复和取消
阶段。已抽出的 `ExecutionService`、`RunObservation`、`InteractionService` 是合理收敛点；后续优先
复用这些流程，保留各业务不同的归属、输出校验和 Journal 更新。暂不引入大而全的基类或运行时包装器。

`RunService` 使用内存字典保存幂等和 run 映射，是内部直连入口；它不具备会话服务的永久恢复保证。
名称相近不代表可互换，本次类注释已明确适用边界。`/tool` 用户消息仍走顶层 Agent，不直接进入直连图。

### 7. kernel 和领域命名仍有历史折中

`kernel/responses.py` 混合核心运行/审批契约与 HTTP 请求响应模型。短期保留可避免广泛导入变更；新增
只服务于 HTTP 的 DTO 应优先留在接口边界，共享运行事实再留在稳定契约层。

`modules/delegation/market_research.py` 把市场研究输入/结果放在委派机制旁，紫微则有独立领域模块。
市场研究出现独立业务规则、数据模型或第二个使用方时，再迁入 `modules/market_research`；当前仅为
三个 DTO 建一个层层嵌套的大模块收益有限。同名的 Ziwei `ArtifactReference`/`ResolvedTarget` 与通用
契约各有语义，应在字段注释和调用处明确含义，避免机械合并。

## 建议实施顺序

1. 收敛契约导出和纯工具函数依赖，补齐实际使用的仓储协议；收益是减少隐藏加载和上层对实现的认知。
2. 修正 Artifact 表归属，保留数据库结构；在扩展交付时收敛 execution/delegation 的跨表事务边界。
3. 按变化频率拆 HTTP 路由/装配及大型应用服务，再决定是否抽出市场研究领域和 HTTP DTO。

每一步单独验证导入、metadata、发布快照和相关流程，不与大范围重命名同时进行。

## 注释与验证边界

本次补充的是职责、状态、字段单位、幂等身份、事务边界、权限上界、时钟基准和证据引用规则，保留
已足够清楚的异常及简单类说明。现有 Pydantic/Tool 输入/输出模型的类 docstring 保持原值，新增解释
使用普通注释，避免可读性改动影响 Schema 描述和已冻结发布快照。

`tests/stage5/test_package_architecture.py` 当前检查 kernel、modules、application 的部分绝对导入禁区，
以及 docstring 是否存在；它不会验证说明内容，也不能完整识别相对导入、聚合导出和传递依赖。
因此“架构测试通过”只能证明现有规则通过，不能证明本文的所有目标边界已经实现。后续强化规则时，
应解析相对导入、列明合法装配入口和跨表事务例外，并限制例外继续增长。

注释复查阶段使用项目现有 `.venv`（Python 3.13.15）验证，结果如下：

| 检查 | 结果 |
|---|---|
| 修改前后 AST 对比（忽略 docstring） | 36 个修改的生产文件一致；涉及 60 个类的注释补充或修正 |
| 相关 Pydantic/BaseTool 类 docstring 对比 | 37 个类全部保持原值 |
| `ruff check financeclaw tests scripts` | 通过 |
| `ruff format --check financeclaw` 与 `git diff --check` | 通过 |
| `pytest -q -m 'not external'` | 206 通过、7 跳过、2 排除、5 个已有失败 |

5 个失败均用修改前保存的生产源码在临时目录中复跑并复现：Stage 1/5 的两个生产配置测试，以及
Stage 7 的三个原文调试保护参数化用例。初次检查时 `FinanceClawSettings.ziwei_allow_full_io` 默认是 True，
与这些测试依赖的默认限制不同；注释复查阶段没有修改该配置或测试。

全仓格式检查还提示工作区原有未跟踪文件 `tests/stage6/test_feishu_lifecycle.py` 的多行字符串引号
需要格式化。它不是本次创建或修改的文件，因此保留原状。

## 回归失败修复

按用户后续要求，将 `FinanceClawSettings.ziwei_allow_full_io` 默认值修正为 False，与
`docs/operations/ziwei-agent.md` 规定的显式开启方式一致。原有 5 个失败共享这个根因：

- Stage 1/5 的两个生产配置用例在 OIDC 检查之前，被默认开启的紫微调试例外拦截；即使紫微未启用，
  配置仍会报 `ziwei_allow_full_io is restricted to development/test`，使正常生产配置无法加载。
- Stage 7 的三个用例分别开启完整日志、取消输入隐藏、取消输出隐藏。默认 True 跳过了保护检查，
  导致本应被拒绝的配置未抛异常。

修复后，development/test 仍可显式设置 `FINANCECLAW_ZIWEI_ALLOW_FULL_IO=true`，staging/production
仍禁止该例外。测试同时改为检查 `ValidationError.errors()` 中的实际校验消息，避免入参回显里的字段名
误匹配；默认行为用例不读取本地 `.env`，紫微用例还清除该例外的环境变量以隔离开发配置。

修复后验证：原失败及显式放行相关的 14 个用例通过；完整本地回归
`pytest -q -m 'not external'` 为 **211 通过、7 跳过、2 排除、0 失败**。
`ruff check financeclaw tests scripts`、本轮修改的 Python 文件格式检查和 `git diff --check` 均通过。
上述 AST 完全一致的结论仅适用于先前注释阶段；本轮唯一生产逻辑改动是该配置默认值。
