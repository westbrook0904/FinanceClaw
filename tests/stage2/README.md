# Stage 2 Reliability Acceptance Tests

本目录是 FinanceClaw 第二阶段最后一步的仓库级验收测试，覆盖：

- End-to-End：Stage 1 Direct Invocation 兼容性；真实内置 Plugin 的
  `finance-review-plan`；Retry + CONTINUE/PARTIAL + Approval + Restart + Resume。
- Fault Injection：transient failure、retry exhausted、unsafe write retry guard、
  timeout、cancel before run、cancel while running、provider exception、invalid provider
  result、checkpoint failure。
- Restart：RUNNING READ crash recovery、非幂等 WRITE fail-closed、Async WAITING
  completion after restart、损坏 SQLite snapshot fail-closed。

测试刻意使用 SQLite 文件数据库验证跨 Engine/跨 Application 重建，不把同一个
`ExecutionEngine` 实例继续使用伪装成 restart。

## 运行

项目要求 Python `>=3.14.3,<3.15`：

```bash
python -m pytest tests/stage2 -v
```

完整阶段二回归：

```bash
python -m pytest -v
```

## 设计原则

1. StateStore snapshot 是恢复事实来源；restart 测试必须创建新的 Engine/Application。
2. 模拟 crash 时保留 crash 前已经落盘的 RUNNING snapshot，再清理旧 asyncio Task，
   避免测试清理动作覆盖恢复输入。
3. READ/NONE 中断允许安全 replay；非幂等 WRITE 不允许猜测执行结果。
4. 用户 Cancellation 是终态，不当作可恢复的 crash。
5. Approval / Async completion 都先持久化终态输入，再复用 Resume。
