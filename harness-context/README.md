# harness-context

该包保留为后续 Conversation Context 迁移的临时边界，只组装受信任请求、MemorySlice 与有界
Observation，并生成不可变 `ContextSnapshot`/`ContextProjection`。

Capability Catalog 来源和旧 Router/Planner/Explorer 投影已从 Stage-1 回归面移除。当前实现不
保存 Prompt、不写 LangGraph checkpoint，也不把原始 Context 写入 Trace。

```bash
.conda/envs/stage0/bin/python -m pytest harness-context/tests -q
```
