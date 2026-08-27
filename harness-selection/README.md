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

当前 Health 保持最小只读模型：Static/Test source、UNHEALTHY hard reject 和 DEGRADED
降级排序。Weighted Canary、Passive Health，以及从 Request/Plan 暴露 Provider Pin
路由入口暂缓；Retry/Fallback 已由 `harness-runtime` 的 ProviderExecutionCoordinator 接管。
