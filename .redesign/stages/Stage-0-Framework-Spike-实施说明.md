# Stage 0：Framework Compatibility Spike 实施说明

## 1. 目标

用最小垂直切片证明 Python 3.13、LangChain、LangGraph Agent Server、LangSmith、PostgreSQL、Redis、MCP 与模型 Provider 可以共同运行，并冻结后续实现依赖版本。

本阶段不迁移真实金融业务，也不删除旧 Runtime。

## 2. 前置条件

- 当前 `main` 测试基线可重复通过；
- 历史备份分支存在；
- 本地具备 Python 3.13；
- 可用的模型 Provider 测试凭证；
- 可访问 LangSmith development project；
- 可启动 PostgreSQL 与 Redis；
- 已明确 Agent Server 本地开发许可边界。

## 3. 依赖与版本

### 3.1 Python

将项目范围调整为：

```toml
requires-python = ">=3.13,<3.14"
```

选择一个包管理/锁定方案并全项目统一，建议使用 `uv.lock`。不允许只写宽泛依赖而没有可重复 lock。

### 3.2 最小依赖

- LangChain；
- LangGraph 与本地 Agent Server/CLI；
- LangSmith；
- 一个实际模型 Provider Integration；
- `langchain-mcp-adapters`；
- PostgreSQL/Redis 所需依赖；
- Pydantic Settings；
- pytest/ruff。

只安装 Spike 用到的 Provider，不一次性引入全生态。

## 4. Spike 切片

创建实验性 graph，不接旧 Capability：

```text
demo_agent
├─ model
├─ read_tool
└─ write_tool
```

要求：

- `read_tool` 可配置 ToolRetryMiddleware；
- `write_tool` 使用 HumanInTheLoopMiddleware；
- Agent 有 thread/checkpoint；
- interrupt 后可以 resume；
- Agent Server 暴露 run/stream；
- LangSmith 显示完整模型和 Tool 子 Run；
- development 日志输出完整最终 Prompt；
- 一个 MCP demo tool 能转换为 BaseTool 并调用。

## 5. 建议文件

```text
financeclaw_spike/
├─ graph.py
├─ tools.py
├─ context.py
└─ settings.py
langgraph.json
```

Spike 代码必须隔离，不得依赖 `harness-runtime/registry/spi`。Stage 1 可以复用已验证的构建片段，但不把临时代码直接当生产架构。

## 6. 验证项

### 6.1 Python 3.13

- 安装全部锁定依赖；
- import smoke；
- async model/tool 调用；
- PostgreSQL/Redis driver；
- Agent Server build/dev；
- 当前仓库可迁移测试的兼容性。

### 6.2 LangChain

- `init_chat_model`；
- `create_agent`；
- Tool Calling；
- Pydantic structured output；
- model retry/fallback；
- tool retry；
- dynamic tool filtering；
- full prompt debug hook。

### 6.3 LangGraph/Agent Server

- thread/run；
- checkpoint；
- interrupt/resume；
- SSE stream；
- worker 重启后恢复；
- Postgres/Redis setup；
- graph/assistant 配置方式。

### 6.4 LangSmith

- development project；
- root/child run tree；
- full inputs/outputs；
- tags/metadata；
- `@traceable` 自定义领域步骤；
- sensitive field masking 演示。

### 6.5 MCP

- stateless MCP session；
- Tool schema/content blocks；
- 本地 governance overlay；
- 不可用/超时行为。

## 7. 测试

- 单元测试：两个 Tool 与 schema；
- Graph 测试：READ 成功、WRITE interrupt、approve/reject；
- 集成测试：Agent Server + PostgreSQL + Redis；
- LangSmith smoke：Trace 包含 Agent、Model、Tool；
- 依赖测试：全新环境从 lock 安装。

## 8. 验收条件

- Python 3.13 全链路稳定运行；
- 依赖 lock 可重复安装；
- Agent Server 可本地启动和构建；
- 多轮 thread 与 checkpoint 可用；
- WRITE Tool 可暂停、审批和恢复；
- READ Tool retry 行为可配置；
- LangSmith 完整显示调用链和 Prompt；
- MCP demo tool 可调用；
- 不需要旧 Harness Runtime 才能完成 Spike。

## 9. 阻断条件

以下任一未解决，不进入 Stage 1：

- Python 3.13 关键包不兼容；
- Agent Server 生产许可/部署路径不可接受且没有替代决议；
- checkpoint/resume 与审批语义无法满足；
- LangSmith 数据策略无法通过安全要求；
- Provider Tool Calling/structured output 不稳定。

## 10. 输出物

- 依赖兼容矩阵；
- 锁定依赖；
- 最小 Agent Server graph；
- LangSmith Trace 截图/链接记录；
- Spike 测试；
- Stage 1 采用的具体 API 版本和差异说明。
