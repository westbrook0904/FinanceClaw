# integration-tests

集成测试覆盖 Direct Invocation、Plan Execution 和 Stage 3A Provider Fabric 三条稳定主链。

Direct Invocation：

```text
Plugin discovery → initialize/register
  ↓
Request → Context → Registry → PRE_EXECUTE Policy → Agent/Tool
  ↓
ResultEnvelope + Trace → unregister/shutdown
```

Plan Execution：

```text
Request + ExecutionPlan
  ↓
PRE_PLAN + PlanValidator + Scheduler
  ↓
CapabilityInvoker + Checkpoint + Plan/Node Trace + Events
  ↓
Approval / Async WAITING → SQLite restart → Resume
  ↓
SUCCESS / PARTIAL / FAILED / DENIED / CANCELLED
```

Provider Fabric：

```text
Registry 1:N → Health-aware Selection → same-provider Retry → controlled Fallback
  ↓
Provider checkpoint → SQLite crash/restart → original Provider replay / WRITE fail-closed
  ↓
ModelGateway → ModelProvider structured output / usage / trace
```

主要测试位置：

- `harness-runtime/tests`：Direct Runtime 和共用 CapabilityInvoker。
- `harness-execution/tests`：DAG、Retry、Cancellation、Resume、Approval、Async、
  Policy、Trace 和 Events。
- `harness-bootstrap/tests`：Composition Root、Application API 和 Plugin 生命周期。
- `plugins/tests`：真实示例插件通过 Bootstrap 的调用。
- `tests/stage2`：finance-review-plan、故障注入、SQLite 重启和损坏状态 fail-closed。
- `tests/stage3a`：multi-provider E2E、WRITE safety、Provider restart、Model Fabric 和旧插件回归。

```bash
.venv/bin/python -m pytest \
  harness-runtime/tests harness-execution/tests harness-bootstrap/tests \
  plugins/tests tests/stage2 tests/stage3a -v
```
