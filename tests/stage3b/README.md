# Stage 3B Routing & Planning Acceptance Tests

本目录是 Stage 3B Step 11 的仓库级阻断验收套件。测试通过公开 Contracts、
Bootstrap/Application API 和各扩展 SPI 组装真实 Registry、Policy、Router、Planner、
ModelGateway、ExecutionEngine、StateStore、Trace 与 Events；所有模型和业务能力均为确定性
本地实现，不访问网络或真实 LLM。

## Gate 矩阵

| 文件 | 阻断场景 |
|---|---|
| `test_execution_mode.py` | 旧 Request 默认 AUTO、模式 round-trip、AUTO explicit target、forced FAST/PLAN、EXPLORE/HYBRID fail-closed |
| `test_rule_routing.py` | deterministic-first、input-type rule、no-match、未知 Capability 零执行 |
| `test_llm_routing.py` | Foundation F1 route-v2 最小 Draft、strict generation、MODEL Trace、失败归一化与零越权执行 |
| `test_llm_planning.py` | 首次有效、invalid→repair→valid、repair exhausted、unknown/cycle/oversized Plan、HybridPlanner 唯一 fallback 条件 |
| `test_handle_lifecycle.py` | 单 Context/Deadline/Trace、完整 Route→Planner→Plan→Provider 关联、WAITING 与跨 Application Resume 不重新 Route/Plan |
| `test_policy.py` | PRE_ROUTE deny、forced mode、Capability/Planner scope 与恶意 Router fail-closed |
| `test_regression_gate.py` | Stage 1 Direct Invocation 兼容、Router/Planner 无 Execution/Provider SPI 依赖 |

非法 Route 或 Plan 的测试同时断言业务 Capability 调用为零；repair exhausted 还断言
StateStore 未创建 checkpoint。Resume Gate 使用临时 SQLite 文件重建 Application，并确认
恢复阶段 Router 与 Planner 调用数保持为零。

## 运行

只运行 Stage 3B Gate：

```bash
.venv/bin/python -m pytest tests/stage3b -v
```

完整 Stage 1 / 2 / 3A / 3B 回归：

```bash
.venv/bin/python -m pytest \
  harness-contracts/tests harness-spi/tests harness-registry/tests \
  harness-plugin-local/tests harness-selection/tests harness-planning/tests \
  harness-policy/tests harness-model/tests harness-routing/tests \
  harness-trace/tests harness-runtime/tests harness-state/tests \
  harness-events/tests harness-execution/tests harness-bootstrap/tests \
  plugins/tests tests/stage2 tests/stage3a tests/stage3b -v
```

验收还要求对变更文件运行 Ruff check/format，并执行 `git diff --check`。按 Stage 3B 当时边界，
EXPLORE/HYBRID 实际执行与 Plan Patch 属于后续 Agentic 阶段，完整 Replay Eval 属于扩展阶段，
均不属于 3B；当前优先级另以 Agent Foundation 一期路线图为准。

Foundation F1 延续本套件作为回归 Gate，但当前规范装配已从内嵌 fallback 收口为
`RoutingPipeline(RuleRouter(), LLMRouter(...))`，且模型输出是最小 route completion Draft，
不再是完整 `RouteDecision`。
