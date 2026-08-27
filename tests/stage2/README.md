# Stage 2 Reliability Acceptance Tests

本目录是 FinanceClaw 第二阶段完成后的仓库级验收套件，验证 Reliable Plan Execution
Engine 的端到端行为，而不是只测试单个类。

## 覆盖范围

- End-to-End：阶段一 Direct Invocation 兼容性；真实内置 Plugin 组成的
  `finance-review-plan`；Retry success、CONTINUE/PARTIAL、Approval、SQLite restart
  与 Resume 的完整链路。
- Fault Injection：transient failure、retry exhausted、unsafe WRITE retry guard、
  timeout、cancel before run、cancel while running、provider exception、invalid provider
  result、checkpoint failure。
- Restart：RUNNING READ 安全重放、非幂等 WRITE fail-closed、Async WAITING 跨重启完成、
  损坏 SQLite snapshot fail-closed。
- Governance/Observability：PRE_PLAN/PRE_EXECUTE、Policy Approval、Plan/Node Trace 和
  Execution Event 的详细覆盖位于 `harness-execution/tests`，并纳入完整回归命令。

测试使用 SQLite 文件数据库验证跨 Engine/跨 Application 重建，不把继续使用同一个
`ExecutionEngine` 实例伪装成 restart。

## 运行

项目要求 Python `>=3.14.3,<3.15`：

```bash
.venv/bin/python -m pytest tests/stage2 -v
```

完整第二阶段回归：

```bash
.venv/bin/python -m pytest \
  harness-contracts/tests harness-spi/tests harness-registry/tests \
  harness-plugin-local/tests harness-planning/tests harness-policy/tests \
  harness-trace/tests harness-runtime/tests harness-state/tests \
  harness-events/tests harness-execution/tests harness-bootstrap/tests \
  plugins/tests tests/stage2 -v
```

## 验收原则

1. StateStore snapshot 是恢复事实来源；restart 必须创建新的 Engine/Application。
2. 模拟 crash 时保留 crash 前已落盘的 RUNNING snapshot，再清理旧 asyncio Task，避免
   测试清理动作覆盖恢复输入。
3. NONE/READ 中断允许安全 replay；非幂等 WRITE 不允许猜测执行结果。
4. 用户 Cancellation 是终态，不当作可恢复 crash。
5. Approval 与 Async completion 都先持久化外部终态，再复用 Resume。
6. Checkpoint 失败时 fail-closed，不允许 Provider 在状态无法保存后继续产生副作用。
7. 所有测试使用确定性本地实现，不依赖网络、真实行情、LLM 或外部服务。
