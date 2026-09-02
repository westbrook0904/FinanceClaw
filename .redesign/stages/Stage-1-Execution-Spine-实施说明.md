# Stage 1：Execution Spine 与 Tool Governance 实施说明

## 1. 目标

建立 FinanceClaw 新的生产运行主干，使默认 Agent、显式 Direct Tool 与 MCP/local Tool 均不依赖旧 Capability Runtime，并在验收后删除第一批重复框架代码。

## 2. 范围

包含：

- FinanceClaw API/BFF 最小入口；
- Agent Server client 与 stream proxy；
- 默认 `finance_agent`；
- `DirectToolGraph`；
- ModelProfile/AgentProfile；
- BaseTool/ManagedTool/ToolGovernance；
- Tool visibility + execution authorization；
- LangChain retry/fallback/HITL；
- LangSmith Trace；
- 最小 Audit。

不包含：完整 Conversation Journal、分层摘要、长期 Memory、真实复杂 Workflow。

## 3. 新模块

建议首先创建：

```text
financeclaw/api
financeclaw/application
financeclaw/agents
financeclaw/graphs
financeclaw/models
financeclaw/tools
financeclaw/audit
financeclaw/infrastructure
```

### 3.1 ModelProfile/ModelFactory

- Profile 配置经 Pydantic Settings 校验；
- 使用 `init_chat_model`；
- fallback 链在 Agent 构建时配置；
- 记录实际模型和 Profile 版本；
- 不出现 ModelProvider SPI。

### 3.2 ToolCatalog

实现不可变 Catalog：

```text
(tool_id, version) → ManagedTool
```

来源：

- 本地 `@tool/StructuredTool`；
- MCP Adapter；
- 金融 Service Tool Adapter。

运行时不支持热注册/注销。MCP server 的治理属性必须由本地配置覆盖。

### 3.3 Tool Policy

实现纯函数或小型无状态 Service：

```text
evaluate(identity, tenant, tool governance, args)
  → ALLOW | DENY | REQUIRE_APPROVAL
```

禁止引入 phase registry、plugin policy SPI 或通用规则语言。

### 3.4 Middleware

- `wrap_model_call`：按权限和 AgentProfile 过滤 Tool；
- 可选 LLM Tool Selector：只能在授权集合内工作；
- `wrap_tool_call`：执行前再次授权、写 Trace/Audit；
- ToolRetryMiddleware：仅作用于允许的 READ/幂等 Tool；
- HumanInTheLoopMiddleware：按 Tool/参数 Policy 触发；
- ModelRetry/ModelFallback；
- Model/Tool call limits。

### 3.5 DirectToolGraph

固定拓扑：

```text
validate_target
  → authorize
  → optional interrupt
  → execute BaseTool
  → project response
```

READ/无需审批 Tool 确定性穿过 interrupt；WRITE Tool 使用 arguments hash 和 approval context。

## 4. API/BFF 最小能力

实现内部可演进的产品接口：

- 默认 Agent run/stream；
- Direct Tool invoke；
- run status；
- approval resume；
- health/readiness。

BFF 负责认证上下文、Target 解析、idempotency 和 Agent Server API 映射，不重建 Run Queue。

## 5. 示例迁移

将现有示例中至少一个 READ Tool 与一个 WRITE/外部动作 Tool 改为 BaseTool。旧 AgentSPI 示例不迁移为新的通用插件；如需保留演示，只作为 AgentProfile/Graph fixture。

## 6. 内部结果与错误

- Agent/Graph 内部使用 BaseMessage、ToolMessage、Command、Interrupt 和框架异常；
- API 边界转换为 DirectToolResponse/AgentResponse；
- 不把 ResultEnvelope 强制套在每次 Tool 调用上；
- 大结果返回 ArtifactReference 预留结构。

## 7. 第一批删除

新链路通过后删除：

- `harness-runtime`；
- `harness-registry`；
- `harness-selection`；
- `harness-spi`；
- `harness-plugin-local`；
- Capability/Provider/Selection/Retry 相关 Contracts；
- 与上述模块绑定的 tests/README/examples；
- root packaging 中对应 package/entry point。

`harness-bootstrap` 在本阶段可先削为空壳或由新 app factory 替换；不得继续引用旧 Runtime。

## 8. 测试

### Unit

- ToolGovernance schema；
- Catalog 版本冲突；
- Policy allow/deny/approval；
- tenant/scope；
- READ/WRITE retry predicate；
- TargetResolver。

### Graph

- Direct READ success/failure/retry；
- WRITE interrupt/approve/reject/edit；
- arguments 修改导致 approval 失效；
- Agent Tool Calling；
- 未授权 Tool 不可见且直接伪造 call 仍被拒绝。

### Integration

- Agent Server run/stream/resume；
- MCP Tool；
- model fallback；
- LangSmith run tree；
- BFF idempotency。

## 9. 验收条件

- 默认 Agent 与 Direct Tool 全部通过新链路；
- Agent/Workflow/Direct 使用同一 Tool Policy 语义；
- Capability/Provider Registry 不在新代码依赖图中；
- WRITE Tool 未批准不能执行；
- READ retry 与 WRITE retry 安全边界通过测试；
- LangSmith Trace 包含授权、模型和 Tool；
- 旧第一批模块删除后测试、lint、packaging 通过。

## 10. 非目标

- 动态插件 Marketplace；
- 通用 Provider Fabric；
- 多 Agent 调度器；
- 声明式 Workflow DSL；
- 完整长期 Memory；
- 生产级所有金融 Tool。
