# FinanceClaw Agent Foundation 一期路线图

> **文档性质**：当前实施优先级 / Phase 1 Delivery Baseline
> **版本**：V1.0
> **日期**：2026-08-31
> **适用范围**：Stage 3C 后续实施、Context Engineering、Memory 以及一期投产验证
> **优先级**：高于旧设计文档中的未来阶段清单

---

## 1. 决策

一期目标不是一次性实现完整的 Agentic Orchestration，而是先交付一个能够真实使用、观察和
迭代的单 Agent 基础闭环：

```text
Request
  ↓
Context Assembly / Projection
  ↓
Route / Plan / Minimal Explore
  ↓
Structured Model Decision
  ↓
Scoped Capability Invocation
  ↓
Observation / Result
  ↓
Governed Memory Read / Write
  ↓
Trace / Basic Evaluation
```

以下能力必须等一期完成并投入真实使用后，再根据运行证据启动新的 ADR：

```text
HYBRID mode 实际执行
PlanPatch / 动态 DAG revision
多 Agent 协作与 delegation
token / cost / 独立 duration 等高阶预算
跨 Provider 成本归一化
复杂分布式 lease / takeover / patch fencing
自动 Workflow 提炼与发布
大规模 Replay / 策略自动优化
```

保留协议枚举或设计草案不等于授权实施。当前 `HYBRID` 继续 fail-closed。

## 2. 为什么先做基础能力

如果没有稳定的上下文工程和记忆机制，继续增加执行模式只会放大以下问题：

- 模型看见什么由调用方临时拼 Prompt 决定，行为无法复现；
- 对话事实、业务事实、执行状态混在一起，无法建立可信边界；
- 每轮重复拉取相同信息，既浪费调用，也降低结果一致性；
- 没有真实使用数据，无法判断 HYBRID 或 PlanPatch 解决的是实际问题还是设计想象；
- 高阶预算缺少 Provider 计量契约和运营基线，只会增加代码复杂度。

因此，一期先把“Agent 如何获得正确上下文、如何安全行动、如何记住必要事实”做扎实。

## 3. 一期能力边界

### 3.1 必须完成

1. 已有 FAST / PLAN 路径继续稳定可用。
2. Strict Structured Output 成为所有模型决策的公共边界。
3. 建立统一 Context Engineering pipeline。
4. 建立 MemoryProvider / MemoryGateway 与基础持久化实现。
5. 实现最小 standalone EXPLORE：串行、每轮最多一个 Action、无 Patch。
6. 所有 Action 经过 Scope、Policy、Schema Validation 与 CapabilityInvoker。
7. 只实现基础次数限制：steps、model calls、action calls、repeat、observations。
8. 建立安全 Trace、基础质量指标和真实业务试用 Gate。
9. Memory、Context、ExecutionState、Secret 明确分离。
10. 默认装配在缺少模型或 Memory 时继续清晰降级或 fail-closed。

### 3.2 一期明确不实现

- HYBRID mode 的 Planner + Explore Node 组合执行；
- Explorer 修改或扩展主 Plan；
- PRE_PATCH、Plan revision CAS 与 Patch-specific recovery；
- 并行 Action、多 Agent、嵌套 Exploration；
- 独立 Exploration duration、token 或 cost budget；
- 向量数据库和自动摘要/压缩策略矩阵；
- 从成功轨迹自动生成 Workflow；
- 为假设中的分布式运行提前设计复杂一致性协议。

已有 Request、Plan、Node 和 Provider timeout/deadline 仍作为执行可靠性边界，不复制进
ExplorationBudget。

## 4. Context Engineering 基础设计

Context Engineering 不是把 `InvocationContext` 整体序列化进 Prompt。它负责把多个可信来源
组装为可验证、可裁剪、可追溯的模型输入。

### 4.1 基础 Contracts

第一版建议只包含：

```text
ContextItem
  item_id
  kind
  content
  source
  provenance
  freshness
  sensitivity
  trust_tier

ContextSnapshot
  snapshot_id
  items
  canonical_hash
  created_at

ContextProjection
  consumer = route | plan | explore
  items
  omitted_item_ids
  projection_hash

ContextUseRecord
  snapshot_hash
  projection_hash
  included_item_ids / omission reasons
```

不使用自由 metadata 传递控制字段。

### 4.2 Pipeline

```text
Trusted System Instructions / Request / Session / Memory / Capability Catalog / Prior Observations
  ↓
ContextSource adapters
  ↓
ContextAssembler（规范化、去重、固定优先级、来源校验；Policy 前仅进程内 candidate）
  ↓
ContextPolicy（租户、身份、敏感级别、用途）
  ↓
ContextSnapshot materialization
  ↓
ContextProjector（按 Router / Planner / Explorer 生成最小视图）
  ↓
Prompt Builder
```

基础边界使用 item count、字符数、单项大小、Observation 数量等确定性限制，不引入 tokenizer
耦合或 token 预算引擎。裁剪顺序必须确定，且记录被省略的 item ID 和原因。

固定信任顺序必须写入 Contract 和测试：trusted system/developer instructions 高于用户输入；
Request、Memory、Tool output 与 Observation 都是数据，不能通过文本内容把自己提升为指令。

### 4.3 必须验证

- 同一输入产生稳定 projection hash；
- Router、Planner、Explorer 看不到不需要的字段；
- Secret、Trace baggage、Provider identity 不进入 Prompt；
- Memory 结果带来源、命名空间和 freshness；
- Prompt / raw response 默认不进入 StateStore 或 Trace；
- Context projection 可以独立单元测试，不依赖真实模型。

## 5. Memory 基础设计

Memory 回答“过去已经知道什么”，StateStore 回答“当前执行到哪里”。两者不得互相替代。

### 5.1 第一版范围

```text
MemoryRecord
MemoryQuery
MemorySlice
MemoryWriteDraft
MemoryWriteProposal
MemoryProvider
MemoryGateway
InMemoryMemoryProvider
SQLiteMemoryProvider（一期真实使用前完成）
```

第一版支持：

- conversation / user preference / domain fact 三类显式记录；
- tenant + subject + namespace 隔离；
- 确定性 filter / tag / text search；
- provenance、created_at、updated_at、expires_at；
- 显式 put / search / delete；
- 通过 Policy 的 MemoryWriteProposal；
- 由 ContextAssembler 消费受裁剪的 MemorySlice。

向量检索、自动 compact、模型自主长期记忆、跨 namespace 联想属于后续优化。

### 5.2 写入原则

允许写入：

- 用户明确提供且允许保留的偏好；
- 有 evidence ref 的稳定业务事实；
- 已完成结果中通过 Schema 和 Policy 的结构化摘要。

禁止写入：

- hidden chain-of-thought；
- Secret、临时 token、未经授权的个人信息；
- RUNNING / WAITING 等执行真相；
- 未验证模型猜测；
- 原始 Prompt、完整 Provider response 或异常堆栈。

Memory 写入失败不得把已经完成的业务 Action 伪装成未执行；结果中应显式报告 memory issue。
模型最多提交不带 tenant/subject/namespace/sensitivity 的 MemoryWriteDraft；可信 Proposal 必须由
Harness 绑定请求身份、命名空间、敏感级别、retention、provenance 与非空 evidence。

## 6. 最小 Agent Loop

一期 Exploration 只实现最容易理解和验证的串行循环：

```text
load ContextSnapshot + MemorySlice
  ↓
check basic count limits
  ↓
strict model decision
  ├── call one capability
  │     ↓
  │   validate scope / input / policy
  │     ↓
  │   checkpoint proposal
  │     ↓
  │   CapabilityInvoker
  │     ↓
  │   bounded Observation
  │
  └── finish with evidence refs
```

约束：

- 每轮恰好一个判别分支；
- 每轮最多一个 Action；
- 固定 `side_effect ∈ {NONE, READ}`、`egress ∈ {NONE, INTERNAL}` 且同步终结；
- 不产生 PlanPatchDraft；
- 不调用另一个 Explorer；
- 不让模型选择 Provider、Plugin 或 Memory implementation；
- resume 只从 completed Observation 边界继续；
- PROPOSED / RUNNING、Approval / Async 或跨 worker 中间态不自动恢复，稳定 fail-closed；
- `HYBRID` 保持不可用。

## 7. 推荐实施顺序

### Foundation 0 — 已完成的前置收口

- fresh plan identity；
- strict structured output；
- retry/fallback slot reservation 与 outbound fencing；
- token usage 仅作遥测。

### Foundation 1 — Routing correctness（已完成，2026-09-01）

- deterministic-first RoutingPipeline；
- 模型只填写未知字段；
- Router / Planner 使用统一 structured generation adapter。

完成证据：显式 `RoutingPipeline` 仅在确定性 Router 返回 `HARNESS.ROUTE.NO_MATCH` 时调用
模型；route-v2 按可信约束动态选择最小 Draft Schema，Harness 自行物化 `source`、
`route_type` 与已知 `mode`；LLMRouter / LLMPlanner 均通过
`StructuredGenerationAdapter` 执行 REQUIRED structured generation。显式 target、固定 PLAN、
确定性 rule 与单一 PLAN Policy 都保持零模型调用。

### Foundation 2 — Context Engineering（已完成，2026-09-01）

- Context Contracts；
- source / assembler / policy / projector；
- Router / Planner 接入；
- redaction 与 deterministic projection tests。

完成证据：新增 `harness-context` 与 Context Contracts；默认 Pipeline 按
`source → normalize/deduplicate/order → PRE_CONTEXT/base guards → Snapshot → consumer
Projection → PromptBuilder` 执行。Router 与 Planner 的模型 Prompt 只消费各自 Projection，
Route/Plan Capability view 分离；Secret、过期项、非法 trust/source 组合及 Policy DENY 在
Snapshot 前移除，PRE_CONTEXT REQUIRE_APPROVAL fail-closed。Snapshot/Projection hash 排除
随机运行身份与收集时间，Trace 只保存 hash、included/omitted 数量和有界 use record。稳定性、
注入隔离、redaction、确定性裁剪与全仓回归均已覆盖。

### Foundation 3 — Memory（已完成，2026-09-01）

- Memory Contracts / SPI / Gateway；
- InMemory 与 SQLite 实现；
- read / write Policy；
- ContextAssembler 接入 MemorySlice；
- namespace、retention、delete tests。

完成证据：新增 Memory Contracts 与 `harness-memory`；`MemoryGateway` 从可信
`InvocationContext` 绑定 tenant/subject，统一执行 namespace、evidence、sensitivity、TTL、
32 KiB record/proposal 与 128 KiB slice 上限，并在 `PRE_MEMORY_READ/WRITE/DELETE` 复用同一
PolicyEngine。InMemory/SQLite Provider 均实现 create-only、稳定 filter/tag/text search、同
proposal identity/hash 幂等和 hash 冲突；get/delete 先加载记录再校验 scope。可选
`MemoryContextSource` 只把受裁剪 MemorySlice 映射为 DATA tier ContextItem；未配置 Provider 时
默认 FAST/PLAN 不增加依赖。隔离、持久化、删除、过期、Policy、注入隔离与跨请求
write→read→ContextProjection 已通过全仓回归。

### Foundation 4 — Minimal Explore

- ExplorationProfile 与基础次数 Budget；
- strict turn draft；
- ScopedActionExecutor；
- standalone EXPLORE；
- Observation-boundary resume；
- 不实现 HYBRID / Patch。

### Foundation 5 — Real-use Gate

- 至少一个真实财经 Agent 场景；
- 使用真实 ModelProvider adapter；
- Context / Memory / Action 全链路 Trace；
- 建立质量、失败、重复动作、memory hit 与人工修正基线；
- 收集实际任务中无法由 FAST / PLAN / standalone EXPLORE 解决的案例。

## 8. 一期投产 Gate

满足以下条件才可讨论二期高阶设计：

1. FAST / PLAN / standalone EXPLORE 均有真实调用记录。
2. Context projection 有稳定来源、裁剪、隔离和泄漏测试。
3. Memory 有持久化、命名空间、授权写入、删除和过期能力。
4. Agent Action 全部经过 CapabilityInvoker，越权调用为零。
5. 基础次数限制和 repeated-action guard 在真实场景验证。
6. 能区分模型问题、上下文问题、记忆问题、工具问题与编排问题。
7. 已形成真实失败案例集，而不是只依赖 Mock 场景。
8. 有明确证据证明单 Agent / PLAN 无法满足某类高价值任务。
9. 至少一个真实场景完成跨请求 Memory write → read → ContextProjection 命中，并验证删除/过期。

二期 ADR 必须引用这些证据，回答“为什么需要 HYBRID / PlanPatch”，而不是只说明技术上可以实现。

## 9. 二期候选，不做预承诺

一期 Gate 通过后，按证据分别评估：

| 候选 | 只有在以下证据出现时考虑 |
|---|---|
| HYBRID | 大量任务同时需要稳定宏观 DAG 和局部未知探索 |
| PlanPatch | standalone Explore 频繁发现必须纳入主 DAG 的新工作 |
| 高阶预算 | 已有可靠 token/cost/latency 计量和明确运营上限 |
| 复杂恢复 | 真实部署采用多 worker，且基础恢复无法满足故障模型 |
| Replay 优化 | 已积累足够真实决策记录，可支持离线策略比较 |
| Workflow 自动化 | 重复成功轨迹稳定、可参数化且具备发布治理需求 |

这些候选彼此独立，不默认打包成一个“大二期”。

## 10. 文档适用规则

- Stage 1 / 2 / 3A / 3B 文档是历史实现基线，不回写历史事实；
- `FinanceClaw-Agent-Foundation-一期实施说明书.md` 是当前 Context、Memory、最小 Explore 契约；
- 旧 Stage 3C Agentic Exploration 文档整体作为高阶设计储备，不直接生成 backlog；
- 第三阶段说明书按本路线图统一为 Foundation F1→F5 顺序，不再让旧 3C / 3D 编号决定优先级；
- 通用架构文档描述长期可能性，不得直接转化为当前 backlog；
- 任意后续高阶能力必须通过新的 ADR 重新进入实施状态。
