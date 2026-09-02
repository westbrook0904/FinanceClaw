# harness-memory

`harness-memory` 是 Agent Foundation F3 的长期事实边界。Memory 回答“跨请求已经获准记住什么”，
不保存当前 Agent/Workflow 的运行状态，也不替代 LangGraph checkpointer。

## 组件

```text
MemoryWriteDraft（模型最多只能提出内容、kind、tags、evidence）
  ↓ Harness 绑定可信 scope / namespace / sensitivity / retention
MemoryWriteProposal
  ↓ MemoryGateway：canonical hash / evidence / size / Policy / TTL
MemoryProvider.put_if_absent

MemoryQuery
  ↓ MemoryGateway：trusted scope / namespace / PRE_MEMORY_READ / stable trim
MemorySlice
  ↓ Stage 3 Model Context 选择器（待接入）
稳定 Memory Slice → 每次模型调用的 ContextManifest
```

- `MemoryProvider`：ID-only 存储 SPI，提供 get/search/put_if_absent/delete；不是公开授权入口。
- `MemoryGateway`：每次操作从可信 `InvocationContext` 派生 tenant/subject，执行隔离、Policy、
  provenance、TTL 与大小边界。
- `InMemoryMemoryProvider`：契约测试与本地开发。
- `SQLiteMemoryProvider`：一期真实试用的单进程持久化基线。
- `MemoryPolicy`：复用统一 PolicyEngine 的 PRE_MEMORY_READ/WRITE/DELETE；Approval 一期
  fail-closed。

## 固定语义

- 模型 Draft 不能设置 tenant、subject、namespace、sensitivity、retention、proposal hash 或
  memory ID；未知字段由契约拒绝。
- 默认只解析当前 `request:<request_id>` evidence；Stage 3 可注入持久化结果 evidence
  resolver，但不能接受任意字符串引用。
- 单条 Proposal/Record 默认最多 32 KiB，单次 MemorySlice 默认最多 128 KiB；配置只能收紧。
- `proposal_id` 在 tenant/subject/namespace 内形成确定性 memory identity；相同 canonical hash
  重复写返回原 Record，不同 hash 冲突。
- Record 是 create-only，`updated_at == created_at`；改变事实需先 scope-checked delete 再创建。
- search 仅提供确定性 kind/tag/text filter；向量检索、自动 compact 和模型摘要不在 F3。
- Secret、过期事实和越界 Provider 结果不会进入 Context；Memory 文本始终是 DATA，不能升级为
  system instruction。

## SQLite

```python
from harness_bootstrap import build_harness
from harness_memory import SQLiteMemoryProvider

app = build_harness(
    memory_provider=SQLiteMemoryProvider("financeclaw-memory.db"),
    memory_namespaces={"profile", "conversation"},
)
```

调用环境仍必须通过自定义 `InvocationContextFactory` 注入可信 TenantContext 与
IdentityContext。未配置 MemoryProvider 时 Bootstrap 不创建 MemoryGateway/Memory Source，既有
FAST/PLAN 路径保持可用。

## 测试

```bash
.venv/bin/python -m pytest harness-memory/tests -v
```
