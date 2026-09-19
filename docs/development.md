# 开发上手与改动导航

本文面向准备阅读或修改 FinanceClaw 的开发者。体验产品请先看[本地完整链路](operations/local-full-stack.md)，所有文档入口见[文档导航](README.md)。

## 1. 准备解释器和依赖

项目要求 Python `>=3.13,<3.14`，版本约束见 [pyproject.toml](../pyproject.toml)；依赖锁在 [uv.lock](../uv.lock)。不要用系统 `python3` 的版本推断项目运行环境。

从仓库根运行：

```bash
uv python install 3.13
uv sync --frozen --extra dev --extra ziwei
uv run --no-sync python --version
uv run --no-sync python -c 'import sys; print(sys.executable)'
```

预期显示 Python 3.13 和项目使用的解释器路径。默认环境为 `.venv`；若主动配置了 `UV_PROJECT_ENVIRONMENT`，以实际输出为准。IDE 应选择同一个解释器。`ziwei` 是可选领域依赖，暂不开发紫微时可以省略 `--extra ziwei`。

已有完整 `.venv` 时，也可使用 `.venv/bin/python`、`.venv/bin/pytest`、`.venv/bin/ruff`；这些直接路径不会自动同步依赖。提示 `uv: command not found` 时先安装 uv，或使用已经验证过的项目环境，不要改用未经核对的系统解释器。

## 2. 先做最小开发验证

包边界与文档契约检查不需要启动 Compose：

```bash
uv run --no-sync pytest tests/stage5/test_package_architecture.py -q
```

它检查服务之间的导入方向、废弃包路径和 Python 定义的 docstring 是否存在。它不证明业务流程、数据库并发或外部服务可用。

随后根据修改范围选择[测试指南](../tests/README.md)中的对应测试。准备交付时对照 [CI 工作流](../.github/workflows/ci.yml)执行完整检查；有外部依赖的测试和探针需使用单独的测试配置。

## 3. 从真实调用链阅读代码

建议先理解[包结构](architecture/package-layout.md)，再沿[请求调用链](architecture/request-lifecycle.md)阅读。无需先遍历全部阶段设计文档。

| 顺序 | 入口 | 阅读重点 |
| --- | --- | --- |
| 1 | [langgraph.json](../langgraph.json) | 哪个根图被发布，产品应用与原生鉴权挂在哪里 |
| 2 | [API 装配](../financeclaw/api/bootstrap.py)和[产品路由](../financeclaw/api/http/routers.py) | 进程启动时创建哪些服务，用户请求如何进入业务用例 |
| 3 | [Turn 受理](../financeclaw/api/application/turns/admission.py) | 身份、幂等键、单会话活动任务和事务边界 |
| 4 | [任务生命周期](../financeclaw/api/application/turns/lifecycle.py)与[原生客户端](../financeclaw/api/application/turns/backend.py) | 命令提交、回执核对、等待与取消如何脱离客户端连接运行 |
| 5 | [根图工厂](../financeclaw/agent_server/graphs/product.py)与 [AgentFactory](../financeclaw/agent_server/agents/factory.py) | 固定发布如何装配模型、工具、上下文与治理中间件 |
| 6 | [结果提交](../financeclaw/api/application/turns/results.py) | 原生状态怎样转为业务状态、Journal 和后续异步工作 |

业务 API 使用的 `get_client(url=None)` 依赖 AgentServer 的进程内环境。完整服务按 Compose 手册启动；单独对 `api.bootstrap` 运行 `uvicorn` 不能替代原生 API、队列和持久化服务。

## 4. 根据需求找改动位置

| 需求 | 首先查看 | 同时核对 |
| --- | --- | --- |
| 修改公开请求 / 响应 | `kernel/` 中的契约、`api/http/` 路由 | 鉴权、幂等、归属与返回状态；[Turn 手册](operations/turn-control.md) |
| 修改任务恢复或取消 | `api/application/turns/`、`shared/turns/` | 不确定提交、租约、原生回执及跨事务边界 |
| 调整模型或任务模型覆盖 | [config/models.toml](../config/models.toml) | [模型手册](operations/model-configuration.md)中的容量与进程配置 |
| 接入同类 HTTP MCP 服务 | [config/mcp.toml](../config/mcp.toml)、`config/mcp/` | [MCP 手册](operations/mcp.md)中的服务身份、固定契约和权限 |
| 新增 / 更新 Skill | `shared/skills/builtin/`、`shared/releases/skills.py` | 包清单、版本、授权、来源、wheel 与 [Skills 发布规则](operations/skills.md) |
| 新增领域 Agent 或 Workflow | `kernel/` 契约、`agent_server/graphs/`、`shared/releases/` | 根任务预算、子图上下文、人机交互与固定发布 |
| 调整上下文或制品读取 | `agent_server/context/`、`shared/artifacts/` | 输入容量、输出预留、内容 hash、读取权限及工具配对 |
| 修改记忆处理 | `shared/memory/`、`memory_worker/`、`integrations/memory_indexer.py` | SQL 事实、证据版本、遗忘状态与投影重放 |
| 调整飞书展示 / 交互 | `shared/channels/feishu/`、`api/application/feishu_*`、`integrations/` | 卡片回调身份、重复提交、投递回执与 [通知手册](operations/notifications.md) |

表中目录均相对于 `financeclaw/`，显式带 `config/` 的路径除外。配置化 MCP 接入通常不需要新增独立客户端；共用策略和发布契约优先复用现有模块。

## 5. 配置、数据与发布的边界

- `.env` 管理应用角色的本地配置；`.env.memory` 单独管理记忆 Worker。具体共享项与专用凭据见[环境说明](../config/environments/README.md)。
- 应用 PostgreSQL 保存业务事实；原生数据库保存 LangGraph 运行数据。应用 Alembic 不管理原生表。
- 初始迁移面向空应用库；已有数据库不能通过重跑同一个初始版本自动补齐字段。先核对对应运行手册的适用范围，不通过删除数据卷修复普通启动问题。
- 模型、工具、技能与策略会进入固定发布。API 与 Worker 需要一致的镜像和配置；已有任务与新发布之间的处理按相应手册执行。
- 受版本管理的技能正文也是运行输入。即使文件扩展名是 `.md`，也不能把修改它等同于修改普通说明文档。

## 6. 更新文档时保持可复现

文档应说明“适用谁、先准备什么、执行什么、怎样判断成功、失败时去哪查”。关键机制用具体状态与源码入口解释，避免只列技术名词。

提交前检查新增链接与锚点、代码示例和配置名；保留外部服务的运行条件。更新现行手册时，不覆盖历史证据中的结果或把原来跳过的验证写成已经通过。项目的展示入口是[根 README](../README.md)，操作细节统一放在本目录。
