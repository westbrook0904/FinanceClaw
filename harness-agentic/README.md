# harness-agentic

`harness-agentic` 是 Agent Foundation F4b 的最小 standalone Exploration 可信边界。它负责
Profile eligibility、canonical facts、单节点 wrapper、strict turn loop、scoped Action、
Observation 和 nested checkpoint 恢复；Planner 与 BasicScheduler 都不解释内部 turn。

## 公共 API

- `ExplorationProfileMaterializer`：从只读 `CapabilityCatalog` 校验模型能力和显式 Action scope，
  只允许 AGENT/TOOL、`NONE/READ` side effect、`NONE/INTERNAL` egress 与显式 `SYNC` completion，
  并冻结收紧后的 `ExplorationProfileSnapshot`。
- `ExplorationPlanFactory`：创建 identity-free、单 `EXPLORATION` node、零 edge、唯一 `/output`
  绑定的 `PlanTemplate`；不分配 `plan_id`，不进入 Scheduler。
- `ExplorationCheckpointValidator`：重验 Profile/scope/proposal/result hash、基础次数、Action/
  Observation 引用及 outer node / inner Exploration 原子一致性；损坏统一返回
  `HARNESS.EXPLORATION.CHECKPOINT_CORRUPT`。
- canonical helpers：稳定 JSON/hash、Profile/Scope/Action/Result facts，以及固定
  `sha256(capability_id + canonical_json(input))` repeated-action fingerprint。
- `ExplorationEngine`：每轮构建 EXPLORE ContextProjection，先 checkpoint ContextUse/model-call，
  再生成一个 REQUIRED structured `call_capability | finish` 决策；执行基础预算、有限 repair、
  evidence 与 completed-Observation resume。
- `ScopedActionExecutor`：在 outbound 前重验 scope、类型、input schema、side-effect、egress 和
  completion mode，并只通过 `CapabilityInvoker` 调用。

## F4b 安全边界

- Profile budget override 只能逐字段收紧，不能在恢复时重新读取配置放宽。
- 旧 Capability Descriptor 未声明 `completion_mode` 时按 `UNKNOWN` 处理，只从 Explore scope
  排除，不影响 FAST/PLAN。
- `PlanValidator(executable=False)` 可校验 Harness-owned wrapper；只有 Composition Root 显式开放
  Explore 后 `executable=True` 才接受，BasicScheduler 始终不会收到该 node。
- 模型 `PlanDraft` 不包含 `EXPLORATION` kind；`ExplorationTurnDraft` 不包含运行身份、Provider、
  Plugin、idempotency、Patch、Approval 或 Async 字段。
- 首次 checkpoint 原子保存 outer RUNNING 与 child RUNNING；Action proposal 先写盘，普通终态、
  Observation 与 pending 清理原子写盘。
- Resume 只接受没有 pending Action、且每个普通终态 Action 都已有 Observation 的边界；
  PROPOSED/RUNNING 返回 `HARNESS.EXPLORATION.RESUME_UNSAFE`。
- Policy approval 不建立 Approval waiting；声明 SYNC 却返回 ACCEPTED 时保存原结果、标记
  orphaned 并失败，不创建 callback/retry。
- EXPLORATION/ACTION Trace 只记录稳定 ID、hash、计数和状态；Prompt、输入、输出和 Provider/
  Plugin identity 不进入这两层 Span。

## Composition Root

```python
app = build_harness(
    exploration_profiles=(profile,),
    single_writer_guaranteed=True,
)
```

缺少任一配置时 EXPLORE 保持不可用。多个 Profile 需要指定 `default_explorer_id`（或由自定义
Router 返回明确的 `explorer_id`）。`memory_required=True` 的 Profile 还要求 ContextPipeline
包含 `MemoryContextSource`。

## 测试

```bash
.venv/bin/python -m pytest harness-agentic/tests -v
```
