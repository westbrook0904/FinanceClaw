# Stage 0：Framework Compatibility Spike 验证记录

更新时间：2026-09-02

状态：实现完成；本地确定性、Agent Server、PostgreSQL/Redis 与镜像构建门禁通过；真实模型
Provider 和 LangSmith 在线门禁等待凭证。因此在补齐在线证据前，Stage 1 仍为 **NO-GO**。

## 1. 实现范围

Stage-0 代码隔离在 `financeclaw_spike/`，包含：

- `create_agent` 构建的 `demo_agent`；
- 可配置 `ToolRetryMiddleware` 的 READ Tool；
- `HumanInTheLoopMiddleware` 保护的 WRITE Tool；
- thread/checkpoint、interrupt、approve/reject、resume 与 stream；
- trusted runtime context 驱动的动态 Tool 过滤；
- OpenAI Provider、Tool Calling 与 Pydantic structured output 在线探针；
- stateless stdio MCP server、`BaseTool` 转换、content blocks 与本地 governance overlay；
- PostgreSQL async driver/checkpointer 与 Redis async driver/Store 探针；
- LangSmith 自动 Agent/Model/Tool runs、自定义 `spike.context.prepare` child run、metadata/tags；
- development 完整 Prompt、Tool Schema、模型 I/O、Tool I/O 日志及 Secret redaction；
- production 环境对 full I/O 和 offline model 的 fail-fast。

静态检查确认 `financeclaw_spike/` 不引用 `harness_runtime`、`harness_registry`、`harness_spi`
或旧 Capability Runtime。

## 2. 冻结版本

完整解析树由仓库根目录 `uv.lock` 固定，共 116 个 package entry。直接相关版本如下：

| 组件 | 冻结版本 | Python 3.13 | 本地结果 |
|---|---:|---:|---|
| CPython | 3.13.15 | 是 | PASS（conda） |
| uv | 0.12.9 | 是 | PASS（frozen sync） |
| LangChain | 1.3.18 | 是 | PASS |
| LangChain OpenAI | 1.6.0 | 是 | 构建 PASS；在线调用待凭证 |
| LangGraph | 1.2.11 | 是 | PASS |
| LangGraph CLI | 0.4.31 | 是 | dev/build PASS |
| LangGraph API | 0.13.3 | 是 | dev PASS |
| LangGraph SDK | 0.4.4 | 是 | PASS |
| LangSmith | 0.12.1 | 是 | import/masking PASS；在线 trace 待凭证 |
| MCP SDK | 1.29.1 | 是 | PASS |
| langchain-mcp-adapters | 0.3.2 | 是 | PASS |
| langgraph-checkpoint-postgres | 3.1.2 | 是 | PASS |
| langgraph-checkpoint-redis | 0.5.2 | 是 | PASS |
| psycopg | 3.3.5 | 是 | PASS |
| redis-py | 7.4.1 | 是 | PASS |
| Pydantic Settings | 2.15.0 | 是 | PASS |
| pytest | 9.1.1 | 是 | PASS |
| Ruff | 0.16.5 | 是 | PASS |

## 3. 已执行验证

### 3.1 确定性测试

```text
187 passed, 3 skipped, 12 subtests passed
```

三个 skip 是无 `OPENAI_API_KEY` 时的真实 Provider 在线调用，以及默认测试运行中未配置的
两个 PostgreSQL/Redis 外部测试；后两项已在容器启动后单独执行通过（`2 passed`）。

该结果包含全部现存 Harness、plugin 回归测试和 Stage-0 测试，证明 Python 3.13 基线未破坏
当前仍需保留的旧功能。

覆盖 READ schema/重试/async、WRITE schema、interrupt、approve、reject、resume、动态工具
过滤、thread checkpoint、v2 stream、MCP 转换/content blocks/timeout、production fail-fast、
完整 I/O redaction、Provider Integration 构建与冻结版本。

### 3.2 Agent Server

`langgraph dev` 使用 Python 3.13 和 offline deterministic model 启动成功。SDK smoke 结果：

```text
stream_parts=12
checkpoint_messages=4
write_interrupted=True
write_resumed=True
```

`langgraph build -t financeclaw-stage0:spike` 成功；`.dockerignore` 生效后的 build context 小于
64 kB，镜像内 Python 3.13 graph factory 加载成功。

### 3.3 PostgreSQL / Redis

使用 `postgres:18-alpine` 与 `redis:8-alpine` 验证：

- async PostgreSQL `SELECT 1`；
- async Redis `PING`；
- `AsyncPostgresSaver.setup()`；
- `AsyncRedisStore.setup()` 和 namespaced put/get；
- WRITE interrupt 后关闭 checkpointer、重建 graph/checkpointer，再 approve/resume。

容器只用于测试，完成后已删除；未创建持久 volume。

### 3.4 MCP

stdio MCP server 能被 `MultiServerMCPClient` 发现并转换为 LangChain `StructuredTool`；每次
调用使用新 session。返回值是标准 text content block list，Tool schema 是 JSON Schema dict。
加载使用显式 timeout，超时映射为稳定的 `MCPToolUnavailable`，远端 schema 不覆盖本地
READ/approval/egress/retry governance。

## 4. 当前 API 差异记录

- `create_agent` 直接返回 `CompiledStateGraph`；Agent Server 配置导出接收
  `RunnableConfig` 的 graph constructor function。
- Agent Server 自己提供 checkpoint/store，因此 server factory 不绑定本地 checkpointer；直接
  Python 测试使用 `InMemorySaver`。
- LangGraph v2 本地调用返回 `GraphOutput.value` 与 `GraphOutput.interrupts`；HITL resume 使用
  `Command(resume={"decisions": [...]})`。
- 当前 HITL interrupt 的 action payload 使用 `args`；审批允许值冻结为 `approve/reject`。
- `ToolRetryMiddleware.max_retries` 表示首次调用之后的重试次数；WRITE Tool 未加入 retry。
- MCP adapter 生成的 `StructuredTool.args_schema` 是 JSON Schema dict，不保证是 Pydantic class。
- Agent Server SDK 0.4.4 的 v2 stream part 在 HTTP 客户端侧按 dict 消费；不要依赖旧版对象属性。

## 5. 尚待外部证据

以下项目不能在没有用户 Secret/组织配置的本地环境伪造：

1. 真实 OpenAI 模型的 async Tool Calling 与 structured output；
2. LangSmith development project 中 Agent → custom context → Model → Tool 完整 run tree；
3. LangSmith inputs/outputs、tags/metadata、masking 和 Dataset/Experiment 记录；
4. Agent Server 生产许可、成本、数据驻留和 egress 评审。

补跑命令与环境变量模板见仓库根目录 `README.md` 和 `.env.stage0.example`。完成后应在本文件
追加 Trace URL/截图位置、执行时间、模型版本和结论，再把 Stage 1 门禁改为 GO。
