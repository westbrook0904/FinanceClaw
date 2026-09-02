# Stage 1：Execution Spine 验证记录

更新时间：2026-09-02

状态：**GO**。Stage-1 新执行主链、离线 Agent Server、DeepSeek OpenAI 兼容协议与 LangSmith
在线 Trace 已通过；第一批重复 Runtime/Registry/SPI 已删除。

## 1. 已落地能力

- `financeclaw/api`：认证适配、FastAPI BFF、SSE、错误映射、health/readiness；
- `financeclaw/application`：TargetResolver、幂等 RunService、薄 Agent Server client；
- `financeclaw/agents`：`create_agent`、权限可见性过滤、执行时二次授权、HITL、retry/fallback；
- `financeclaw/graphs`：默认 `finance_agent` 和固定拓扑 `direct_tool`；
- `financeclaw/models`：不可变 ModelProfile/Catalog 与基于 `init_chat_model` 的 ModelFactory；
- `financeclaw/tools`：local/MCP BaseTool、不可变 Catalog、ToolGovernance、纯 ToolPolicy；
- `financeclaw/audit`：有界、无 Secret、append-only 的最小 Audit port 与本地实现；
- `financeclaw/infrastructure`：Pydantic Settings、DeepSeek OpenAI 兼容端点和生产 fail-fast。

默认入口 `main:app` 指向新 BFF；`langgraph.json` 只导出 `finance_agent` 与 `direct_tool`。

## 2. 安全与执行语义

- identity/tenant/scopes 只来自认证 adapter 生成的受信任 `ExecutionContext`，请求 body 不能覆盖；
- Agent 只能看到 AgentProfile allowlist 与当前 Policy 均允许的 Tool；
- 任意实际 Tool call 在执行前再次授权，伪造/越权 call 返回拒绝消息；
- WRITE Tool 固定要求审批且不重试；edit 改变 arguments hash 后重新 validate/authorize/interrupt；
- READ Tool 只对声明的 transient error 执行有界 retry；
- 模型 retry 在 fallback 之前执行，fallback 还需满足数据等级、区域与能力约束；
- Trace metadata 使用 tenant/subject hash；Audit 保存 payload hash，不保存参数与凭证原文。

## 3. 自动化验证

Stage-1 测试：

```text
22 passed
```

保留的 Context/Memory/Policy/Trace/Events 回归：

```text
41 passed
```

仓库根测试（Stage-0 + Stage-1）：

```text
39 passed, 3 skipped
```

Ruff 对新旧保留代码和测试：

```text
All checks passed
112 files already formatted
```

editable wheel 构建/安装：

```text
Successfully built financeclaw
Successfully installed financeclaw-0.1.0
```

## 4. Agent Server 真实进程联调

使用 `langgraph dev`、Python 3.13、offline deterministic model 启动真实本地 Agent Server，
SDK smoke 结果：

```text
finance_agent_stream_parts=18
direct_read_succeeded=True
write_interrupted=True
edit_reinterrupted=True
write_approved=True
```

该结果证明 graph factory 可由 CLI 加载，并实际穿过 thread/run/stream/interrupt/resume API。

## 5. DeepSeek 与 LangSmith 在线证据

配置：OpenAI Provider Integration + `https://api.deepseek.com` + 本地 Secret 注入。在线探针结果：

```text
model_type=openai-chat
tool_calling=True
structured_output={"symbol":"AAPL","should_read":true}
governed_tool_executed=True
```

LangSmith run tree：

- [stage1.provider.probe trace](https://smith.langchain.com/o/e9f4de3d-dec3-417a-8e9e-5d2a100d0f4f/projects/p/4b88a52a-dde2-46a1-9060-86ec0dbc931d/trace/01a06272-2a0e-7cc1-9d96-83a03fd66612/run/01a06272-2a0e-7cc1-9d96-83a03fd66612?start_time=2026-09-02T14%3A07%3A24.174282%2B00%3A00)

Trace 由真实 Agent 调用生成，包含模型调用、`context.prepare`、`tool.authorization` 与
`market_snapshot` Tool execution；本地 Audit 同时确认 `financial_tool.executed`。

兼容性差异：该 DeepSeek thinking 模型拒绝默认原生 JSON Schema `response_format`，也拒绝
function-calling structured output 所需的强制 `tool_choice`；使用 OpenAI-compatible JSON mode
成功完成 Pydantic structured output。普通 Tool Calling 的 auto choice 正常。

## 6. 第一批删除与依赖隔离

已删除 `harness-runtime`、`harness-registry`、`harness-selection`、`harness-spi`、
`harness-plugin-local`、旧 `harness-bootstrap`、示例 plugins、Stage-3A Provider Fabric 以及
Capability/Provider/Selection/Retry/Result 相关 contracts、测试、README 和 packaging entry。

静态依赖测试会扫描 `financeclaw/`，禁止重新引用上述包；新主链直接使用 LangChain/LangGraph
原生对象，并仅在 BFF/Audit 边界投影 FinanceClaw DTO。

## 7. 后续边界

当前 Audit repository 是 Stage-1 的进程内最小实现；永久 PostgreSQL Audit、生产 OIDC/JWT、
部署级 Agent Server auth、完整 Conversation Journal 和 Artifact Store 按后续 Stage 落地。
