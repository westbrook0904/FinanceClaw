# harness-context

`harness-context` 是 Agent Foundation F2 的统一 Context Engineering 边界。它把受信任系统
指令、请求投影、受治理 MemorySlice 与 Capability Catalog 等来源组装成不可变
`ContextSnapshot`，再按
Router、Planner 或 Explorer 生成最小 `ContextProjection`；它不保存 Prompt、不写
StateStore，也不把原始 Context 写入 Trace。

## Pipeline

```text
ContextSource candidates（仅进程内）
  ↓
ContextAssembler.normalize（来源校验、去重、固定排序）
  ↓
ContextPolicy（不可放宽的 trust/sensitivity/expiry guard + PolicyEngine PRE_CONTEXT）
  ↓
ContextSnapshot
  ↓
ContextProjector（consumer allowlist + deterministic limits）
  ↓
ContextProjection / ContextUseRecord
  ↓
PromptBuilder
```

默认 `ContextPipeline` 提供 `RequestContextSource` 与
`CapabilityCatalogContextSource`。Route 目录只包含 Capability 的
id/name/type/version/tags；Plan/Explore 才增加 input/output schema 与 execution profile。F4a 的
`completion_mode` 只进入 EXPLORE 视图；PLAN 继续使用原有执行画像投影，Explore eligibility 则由
Harness 在模型调用外强制执行。
Descriptor metadata、Provider、Plugin、Identity、Tenant attributes 与 Trace baggage 不进入
模型 Prompt。

Bootstrap 配置 MemoryProvider/Gateway 时，会追加 `MemoryContextSource`。该 Source 只调用
Gateway，不直接访问 Provider；每条 MemoryRecord 保留 namespace、kind、tags、source fact hash、
provenance、freshness/expiry，并固定映射为 DATA tier。未配置 Provider，或最小 Invocation 缺少
可信 tenant/identity 且 Memory 非 required 时，Memory candidate 为空。

## 安全与确定性

- `ContextItem.item_id` 由稳定 source identity、source version 与 kind 派生。
- Snapshot hash 排除 snapshot ID、收集时间与 Trace identity；Projection hash 还包含 consumer
  与固定 omission reason。
- Policy 在 Snapshot 物化前执行；Secret、过期项和非法 trust/source 组合直接丢弃。
- PRE_CONTEXT Policy 只能进一步收紧；DENY 过滤 item，REQUIRE_APPROVAL fail-closed。
- 裁剪只使用 `max_items/max_chars/max_chars_per_item/max_observations/max_memory_records`，不依赖
  tokenizer。
- Omission 只保存 item ID 与 reason code，不复制被裁剪内容。
- 只有 SYSTEM trust 的 system-instruction source 能生成 system message；Request、Memory 与
  Observation 中的指令文本始终作为数据。

`PromptBuilder` 只向模型暴露 consumer、source kind、trust tier、kind 和 content。运行时
item/source/use/snapshot ID、source version、sensitivity 与 projection hash 留在 Harness 观察面。

## Bootstrap 接入

`build_harness()` 默认构造与全局 `PolicyEngine` 共用实例的 `ContextPipeline`。调用方可注入
自定义 Pipeline，但其 `ContextPolicy.policy_engine` 必须与 Harness 的 PolicyEngine 相同。
同时配置 MemoryGateway 时，自定义 Pipeline 必须显式包含引用同一 Gateway 的
`MemoryContextSource`。
RequestCoordinator 在 Router 与 Planner 调用前分别生成 ROUTE / PLAN Projection；对应 Span
只记录 snapshot/projection SHA-256 与 included/omitted 数量。

## 测试

```bash
.venv/bin/python -m pytest harness-context/tests -v
```
