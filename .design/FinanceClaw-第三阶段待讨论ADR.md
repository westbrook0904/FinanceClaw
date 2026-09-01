# Stage 3 ADR 状态摘要

> **状态日期**：2026-09-01
> **当前优先级来源**：`.design/FinanceClaw-Agent-Foundation-一期路线图.md`
> **当前实施契约**：`.design/FinanceClaw-Agent-Foundation-一期实施说明书.md`

Stage 3A 与 Stage 3B 已完成。Stage 3C 的 Plan Identity 与 Strict Structured Output 两个前置
步骤已完成；其余设计不再视为整体冻结。当前决议是先完成 Agent Foundation 并真实使用，
HYBRID、PlanPatch、高阶预算等转为 future design。

## 已决议并实现

1. **ExecutionMode 的归属**
   - 唯一持久化位置为 `RequestOptions.execution_mode`。
   - `handle(..., mode=...)` 仅把 sugar 归一化到不可变 Request 副本。
   - 当前实际执行 AUTO / FAST / PLAN；EXPLORE 尚未完成，HYBRID 继续 fail-closed。

2. **Router / Planner 权限**
   - Router 只产生 Route proposal / decision，不能选择 Provider 或执行 Capability。
   - 模型只产生 identity-free PlanDraft；Harness 分配 plan_id / revision 并验证 ExecutionPlan。
   - Planner 由服务端配置与 Policy 选择，不允许模型选择。

3. **Planner 组合与 WorkflowSPI**
   - 现有 `HybridPlanner` 只是 primary `NOT_APPLICABLE` 后的 fallback Planner，不等于
     `ExecutionMode.HYBRID`；后续可考虑改名 `FallbackPlanner` / `CascadingPlanner`。
   - 一期不引入 WorkflowSPI。

4. **ModelProvider 调用边界**
   - ModelProvider 使用独立 SPI + ModelGateway。
   - ModelGateway 复用 Registry、Selection/Health、ProviderExecutionCoordinator、Trace/Events，
     但不把 GenerateRequest 伪装成 Agent/Tool 请求。

5. **WRITE Fallback**
   - 只有稳定 idempotency key 与相同非空 equivalence_group 同时满足时，才允许跨 Provider
     自动 fallback；其他 WRITE fail-closed。

6. **Stage 3C 已完成前置步骤**
   - plan_id 是 fresh execution identity，Planner candidate 不携带最终运行身份。
   - Strict Structured Output 使用 Provider-native constraint + 本地完整校验 + 业务校验。
   - generation reservation 只冻结 retry/fallback slots 和 fencing，不包含 token/cost 上界。
   - ModelUsage 仅作遥测，不参与预算或准入。

## ADR-P3-F-001：Agent Foundation 优先

**决议：接受。**

一期实施顺序：

```text
Routing correctness
  ↓
Context Engineering
  ↓
Memory Foundation
  ↓
Minimal standalone EXPLORE
  ↓
真实业务试用 Gate
```

理由：当前只有 InvocationContext 和局部 Request projection，没有跨 Router / Planner /
Explorer 的统一上下文工程；Memory 又被旧路线排在高阶编排之后。继续实现 HYBRID / Patch 会
在缺少真实 Agent 使用基础时放大复杂度。

## ADR-P3-F-002：Context Engineering 是一等基础能力

**决议：接受。**

- `InvocationContext` 只表示身份、租户、trace、deadline 等执行元数据；
- `ExecutionState` 是执行真相；
- `ContextSnapshot / ContextProjection` 是模型可见输入；
- `MemorySlice` 是经检索、Policy 和裁剪后的历史事实。

统一采用：

```text
ContextSource → transient assembly → ContextPolicy → ContextSnapshot → ContextProjector → PromptBuilder
```

Context item 必须带 trust tier、source/provenance、freshness 与 sensitivity。Memory、用户输入、
Tool output 都是数据，不能提升为系统指令。一期使用条目数/字符数做确定性裁剪。

## ADR-P3-F-003：Memory 前移并与 StateStore 分离

**决议：接受。**

一期实现 MemoryProvider / MemoryGateway、InMemory / SQLite Provider、namespace 隔离、TTL、
provenance、显式 search/put/delete 和 Policy-gated MemoryWriteProposal。

Memory 不保存执行状态、hidden CoT、Secret、未经验证模型猜测或原始 Provider response。
Router / Planner / Explorer 不能直接查询 MemoryProvider，只消费 Context pipeline 生成的
MemorySlice。

## ADR-P3-F-004：最小 standalone EXPLORE

**决议：接受。**

- Harness-owned ExplorationEngine；
- 每轮严格 `call_one_capability | finish`；
- 串行、每轮一个 Action；
- 仅允许显式声明同步终结的 `side_effect ∈ {NONE, READ}`、`egress ∈ {NONE, INTERNAL}` Capability；
- 所有 Action 经 Scope、Schema、Policy 与 CapabilityInvoker；
- 仅保留 steps/model_calls/action_calls/repeat/observations 基础次数限制；
- 只从 completed Observation 边界恢复；PROPOSED/RUNNING、Approval/Async、跨 worker 中间态
  均 fail-closed；
- 声明同步但意外返回 ACCEPTED 的 Provider 记为 ORPHANED/FAILED，不接 callback；
- 不允许 PlanPatchDraft 或 nested exploration。

## ADR-P3-F-005：高阶模型预算 Contract 作 pre-release corrective break

**决议：接受。**

`9848544` 中曾提前加入 `NormalizedCost*`、token/cost reservation upper bound、
`ModelAttemptPolicy` budget fields 与 Provider token-bound SPI。它们尚未形成已发布的稳定版本，
仓库也没有持久化 reservation 的生产消费者；Agent Foundation 一期不具备可靠跨 Provider 成本
计量和运营上限，因此删除这些字段，不保留 deprecated shim，也不提供旧 reservation JSON
迁移器。

这是一次明确的 pre-release breaking correction：

- 旧 Python import 与旧 reservation wire payload 不再兼容；
- 任何外部试用方必须重新生成 reservation，不得读取或续跑旧快照；
- `ModelUsage` 仅为可选遥测，不参与 Provider eligibility、generation 成败或预算；
- `max_output_tokens` 仍是模型请求参数，不是 Harness 资源预留；
- 若未来重新引入 token/cost budget，必须基于真实计量数据单独 ADR，不能恢复未验证字段。

## ADR-P3-F-006：reserved generation 绑定授权上下文与 Provider incarnation

**决议：接受。**

reservation 除 request/schema/slot 快照外，还必须绑定：

```text
trusted Request
IdentityContext（subject / scopes / attributes）
TenantContext
plan_id / node_id / exploration_id
Provider registration version + process-local provider incarnation
```

execute 前任一授权上下文 hash 不一致，返回 `MODEL_RECEIPT_MISMATCH` 且 outbound=0；同 descriptor /
features 的 Provider 实例被热替换时 incarnation 必须变化，旧 reservation 返回
`MODEL_GENERATION_ORPHANED` 且新旧 Provider 均不得收到该调用。trace、cancellation snapshot 和可
进一步收紧的 deadline 不进入授权 hash。

## 延后决议：一期投产后重新 ADR

以下内容保留设计材料，但当前状态统一为 **DEFERRED**：

- `ExecutionMode.HYBRID` 实际执行；
- PlanPatch、PRE_PATCH、动态 DAG revision；
- Exploration 内 Approval / Async / WRITE 自动恢复；
- operation claim、lease takeover、multi-worker fencing；
- token / cost / 独立 duration budget 与跨 Provider 成本归一化；
- parallel action、nested exploration、多 Agent；
- Selection / Route / Plan / Explorer 完整 Replay Eval；
- 自动 Workflow 提炼与发布。

延期项不能因为 Contract 或旧章节已经存在而直接进入实现。必须在一期投产 Gate 后引用真实
失败案例，重新说明收益、边界和最小实现。

## 二期评审入口

只有以下事实同时成立，才开始高阶模式 ADR：

1. Context 与 Memory 已投入真实业务使用；
2. standalone EXPLORE 有稳定运行记录和失败分类；
3. 有证据证明 FAST / PLAN / standalone EXPLORE 无法满足某类高价值任务；
4. 高阶方案解决的实际问题、复杂度和运维成本可以量化；
5. 仍能保持模型无执行权、Memory != StateStore 与 Plugin 无 Service Locator 等红线。
