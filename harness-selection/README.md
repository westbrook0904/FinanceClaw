# harness-selection

Stage 3A 的 Provider 数据面选择模块。它只负责：

```text
Registry candidates
        ↓
Eligibility
        ↓
Health-aware ranking
        ↓
SelectionDecision
```

本模块不执行 Provider，也不修改 Registry、StateStore 或 ExecutionPlan。

## 当前范围

- `HealthSource`
- `StaticHealthSource`
- `TestHealthSource`
- `EligibilityPipeline`
- `PrioritySelector`
- Tenant visibility
- Policy provider/region/tag constraints
- Provider Pin eligibility
- `HEALTHY / UNKNOWN / DEGRADED / UNHEALTHY`
- 稳定 `selection_key`

PrioritySelector 的排序顺序固定为：

```text
HEALTHY > UNKNOWN > DEGRADED
        ↓
priority descending
        ↓
provider_id ascending
```

`UNHEALTHY` 不进入排名。

Canary、Passive Health、Retry/Fallback 和 Runtime 接入属于后续 Step。
