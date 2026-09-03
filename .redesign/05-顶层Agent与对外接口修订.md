# 顶层 Agent 与对外接口修订

状态：Accepted

日期：2026-09-03

## 1. 修订原因

此前设计把 `ToolTarget`、`WorkflowTarget` 和 `AgentTarget` 暴露给产品调用方，导致两类问题：

- 用户可以绕过顶层 Agent 的意图理解、参数提取和提槽，直接选择执行图；
- Conversation 被建模成“绑定用户指定 Agent”，混淆了根会话所有者与领域任务执行者。

本修订以“一个根会话、一个顶层 Agent、多个受控能力”为统一模型。它覆盖并替代 RD-002、RD-003、RD-013 和 RD-028 中与公开 Target 有关的原表述。

## 2. 统一运行语义

```text
User message
    │
    ▼
finance_agent（根会话唯一所有者，ReAct）
    ├─ 直接回答
    ├─ Tool Calling ───────────→ BaseTool
    ├─ Workflow handoff ───────→ published StateGraph child run
    └─ Agent delegation ───────→ domain Agent child run
```

顶层 Agent 负责意图判断和能力选择，但不负责授权决策。Catalog/Profile 决定候选集合，Policy 决定当前身份可见和可执行的集合，执行节点在真正调用前再次授权。

领域 Agent 只接收一个被限定范围的任务和必要上下文。它不能替换根 Agent、接管 Conversation、扩大权限，或把自己的结果直接伪装成最终答复；结果回到顶层 Agent 后再形成用户可见回复。

## 3. 显式调用指令与提槽

支持以下消息语法：

```text
/tool <tool_id> [自然语言参数 | JSON object]
/workflow <workflow_id> [自然语言参数 | JSON object]
/agent <domain_agent_id> <任务描述>
```

指令属于用户消息，不属于可信 Runtime Context。处理顺序固定为：

1. 解析指令类型和资源 ID；
2. 在当前 AgentProfile、Catalog 和 Policy 过滤后的集合中解析资源；
3. 使用发布版本的 Pydantic Schema 提取并校验参数；
4. 参数缺失或无效时记录 slot 状态，只询问缺少/无效的字段，不启动执行；
5. 参数完整时调用指定能力，不静默改选其他能力；
6. Tool、Workflow 或领域 Agent 在执行边界再次做 scope、tenant、egress、审批和幂等校验。

示例：

```text
User: /tool market_snapshot
Agent: 请提供 symbol。
User: AAPL
Agent: [调用 market_snapshot(symbol="AAPL")]
```

```text
User: /tool calculate {"operation":"multiply","left":2,"right":3}
Agent: [Schema 校验通过，直接调用 calculate]
```

指令不能指定版本、tenant、subject、scope、审批结果、模型或凭证。版本由 Catalog 发布状态解析并在 run/manifest/audit 中固定。

## 4. 对外 API

产品写入口只保留会话和消息：

```http
POST /v1/conversations
{}

POST /v1/conversations/{conversation_id}/turns
Idempotency-Key: <key>
{"message": "..."}
```

公开请求体不含 `agent_id`、`agent_profile_version`、`target`、`tool_id` 或 `workflow_id`。Conversation 响应也不把内部 AgentProfile pin 当作产品配置暴露。

运行生命周期接口继续按 opaque `run_id` 工作：

```text
GET  /v1/runs/{run_id}
GET  /v1/runs/{run_id}/events
POST /v1/runs/{run_id}/resume
```

`POST /v1/runs`、`POST /v1/tools/{id}:invoke` 和 `POST /v1/workflows/{id}/runs` 不属于公开产品 Contract。迁移期若保留，只允许具有 `internal:invoke` scope 的服务身份访问，并从 OpenAPI 隐藏；最终在内部 handoff 完成后删除兼容路由。

## 5. Workflow 与领域 Agent handoff

Workflow 和领域 Agent 应以受治理的 delegation capability 暴露给顶层 Agent，而不是公开路由：

- capability 提供稳定 ID、描述和输入 Schema；
- 顶层 Agent 生成 typed handoff request；
- BFF/Graph orchestration node 再解析发布版本并创建 child thread/run；
- `delegation_record` 永久关联 parent run、child run、目标版本、参数 hash 和权限决策；
- child 需要审批时沿用现有 interrupt/resume，不能由父 Agent 自动批准；
- child 完成后，结构化结果或 ArtifactReference 返回父 run，由顶层 Agent 汇总。

不能把 Workflow 全图以内联 Tool 函数方式塞进顶层 ReAct 循环，否则会破坏独立 checkpoint、恢复、审批和业务 run 映射。

## 6. Conversation 内部固定项

业务数据库仍保存根 `agent_id`、`agent_profile_version` 和 `agent_thread_id`，用途仅限：

- 重启恢复与结果对账；
- Prompt/ModelContextManifest 可复现；
- 灰度升级和审计。

这些字段由部署配置写入，不接受请求覆盖。升级根 Agent 必须通过显式迁移或新建 Conversation 完成。

## 7. 实施顺序

1. 收敛 Conversation/Turn API，隐藏并保护旧直接执行路由；
2. 增加 slash parser、Tool Schema 提槽和单 Tool 约束；
3. 把 published Workflow 注册为 typed delegation capability，并持久化父子 run 映射；
4. 在确有领域边界和评测集时注册 domain Agent delegation；
5. 删除内部兼容 `RunRequest/TargetResolver/DirectToolGraph` 中不再承担职责的部分。

每一步都必须增加权限绕过、缺参、多轮补槽、错误目标、不静默替换和跨租户隔离测试。
