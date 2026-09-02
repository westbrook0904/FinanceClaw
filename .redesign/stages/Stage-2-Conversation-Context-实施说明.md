# Stage 2：Conversation Journal 与 Context 实施说明

## 1. 目标

完成永久多轮会话、Agent Server Thread 映射、长上下文控制、分段/分层摘要、相关历史召回、ModelContextManifest 和调试完整 Prompt，并删除旧 Context Runtime。

## 2. 数据库准备

使用 SQLAlchemy/Alembic 创建：

- conversations；
- conversation_turns；
- conversation_messages；
- conversation_summaries；
- model_context_manifests；
- artifact metadata 基础表。

建立 tenant/subject/ownership、sequence、run_id 和 idempotency key 索引/唯一约束。

## 3. Conversation Journal

### 3.1 写入规则

1. 验证身份和 Conversation ownership；
2. 生成/校验 idempotency key；
3. 写入用户消息；
4. 调用 Agent Server thread run；
5. 投影 SSE；
6. 写入 Assistant 可见最终消息；
7. 写 Manifest/Audit；
8. 失败时保留可恢复状态。

所有可见原文 append-only，无自动 TTL。重新生成追加带 parent_message_id 的分支。

### 3.2 Thread 映射

默认 Agent：`conversation_id ↔ agent_thread_id`。Conversation 创建时固定 AgentProfile version。BFF 调用 Agent Server 前必须校验映射所有权。

## 4. Context 类型

实施时严格区分：

- Runtime Context：identity/tenant/services，对模型隐藏；
- Agent State：messages/tool results/pending state，由 Checkpointer 管；
- Model Context：每次 model call 的实际 messages/tools/system prompt；
- Tool Context：ToolRuntime 中的 state/context/store/execution info。

不再生成独立的 ContextSnapshot/Projection DTO。

## 5. Prompt 预算算法

计算：

```text
available_input_tokens
= model input limit
- reserved output tokens
- system/policy reserve
- tool schema reserve
- safety margin
```

按优先级放入：

1. system/mandatory policy；
2. current user input；
3. current tool results/pending state；
4. recent raw messages；
5. stable memory（Stage 3 接入，当前留接口）；
6. relevant summaries；
7. relevant older raw messages；
8. optional background。

裁剪必须记录 omission reason 与 token counts。

## 6. Summary

### 6.1 Segment Summary

当历史超过模型/profile 配置阈值时，对已关闭的连续 turn 区间生成摘要。摘要保留 source message IDs、range、model/template versions 和 hash。

### 6.2 Hierarchical Summary

当 segment summary 数量继续增长时，可以在不删除原 segment 的前提下生成 higher-level summary。避免反复覆盖同一 rolling summary 导致漂移。

### 6.3 Retrieval

Prompt 同时选择最近窗口和与当前输入相关的 summary/older messages。第一版可 metadata + text search；语义召回在 pgvector Spike 后启用。

金融历史数字必须标记为历史陈述；需要当前事实时由 Agent 调用金融 Tool 重新查询。

## 7. ModelContextManifest

每次 model call 记录：

- conversation/turn/run/model call IDs；
- Agent/Model/Prompt versions；
- recent message range；
- summary/memory/history/tool result refs；
- exposed Tool IDs/versions；
- input token count；
- omissions；
- context hash。

Manifest 永久保存且有界。完整 Prompt 默认只在 development LangSmith/Debug Log；高风险 Workflow 可保存加密 Prompt Artifact。

## 8. Debug Model I/O

实现 `DebugModelIOMiddleware` 或等价 LangChain hook：

- 输出最终 system/messages/tools/response format；
- 输出模型/Profile、token budget、ContextManifest；
- 输出模型可见 response/tool calls；
- 使用 model_call_id 关联 LangSmith；
- development/test 显式开关；
- production 误开 fail fast/强制关闭；
- 永不输出 credential/secret/hidden reasoning。

## 9. 大 Tool Result

建立 Artifact abstraction。超过上下文阈值的 Tool Result：

1. 保存完整内容到 Artifact Store；
2. ToolMessage 仅包含摘要、artifact_id、hash、source/as_of；
3. Manifest 保存 reference；
4. 授权 Tool 才能重新读取 Artifact。

## 10. 第二批删除

新 Context 链路稳定后删除：

- `harness-context`；
- `ContextItem/ContextSource/ContextSnapshot/ContextProjection`；
- `ContextConsumer.ROUTE/PLAN/EXPLORE`；
- `ContextAssembler/Projector/PromptBuilder`；
- Capability Catalog Context Source；
- 旧 Observation Context 投影；
- 对应测试、README 和 bootstrap wiring。

可保留的语义必须已迁移到 Middleware、Manifest、Conversation 或 Audit，而不是继续保留旧包。

## 11. 测试

- Journal append-only/branch/idempotency；
- Conversation ownership/tenant；
- Thread 映射；
- AgentProfile version pin；
- 最近窗口 token budget；
- segment/hierarchical summary；
- relevant old history recall；
- summary provenance/rebuild；
- ContextManifest completeness；
- Debug full prompt 开关；
- Secret 不进入日志；
- Artifact offload；
- Agent Server 重启与 turn reconciliation。

## 12. 验收条件

- 多轮会话跨进程继续；
- 原始对话不因摘要或 State compaction 丢失；
- 长会话不超过模型输入窗口；
- 相关古老历史可召回；
- Manifest 可解释每次模型调用使用的上下文；
- development 可查看完整 Prompt；
- production 默认不泄露敏感 I/O；
- 删除旧 Context 包后测试、lint、packaging 通过。
