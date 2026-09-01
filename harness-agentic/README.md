# harness-agentic

`harness-agentic` 是 Agent Foundation F4a 的最小 Exploration 可信边界。当前只负责 Profile
eligibility、canonical facts、standalone wrapper 的结构物化与 nested checkpoint 完整性校验；
不执行模型 turn，不调用 Capability，也不开放 EXPLORE handle。

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

## F4a 安全边界

- Profile budget override 只能逐字段收紧，不能在恢复时重新读取配置放宽。
- 旧 Capability Descriptor 未声明 `completion_mode` 时按 `UNKNOWN` 处理，只从 Explore scope
  排除，不影响 FAST/PLAN。
- `PlanValidator(executable=False)` 可校验 Harness-owned wrapper；默认 `executable=True` 返回
  `PLAN.EXPLORATION_NOT_AVAILABLE`，阻止 Stage 2 Scheduler 把新 node 当普通 Capability 执行。
- 模型 `PlanDraft` 不包含 `EXPLORATION` kind；`ExplorationTurnDraft` 不包含运行身份、Provider、
  Plugin、idempotency、Patch、Approval 或 Async 字段。
- 当前没有 `ExplorationEngine`、`ScopedActionExecutor`、Observation-boundary resume、Trace 接入或
  Composition Root 开关；这些属于 F4b。

## 测试

```bash
.venv/bin/python -m pytest harness-agentic/tests -v
```
