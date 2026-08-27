# Stage 3A Provider Fabric Acceptance Tests

本目录是 Step 9 的仓库级阻断验收套件。测试只通过公开 Bootstrap/Application API 组装
真实 Registry、Selector、Invoker、ExecutionEngine、StateStore、Trace、Events 和
ModelGateway，不把单个组件的单元测试冒充端到端验收。

## Gate 矩阵

| 场景 | 验收点 |
|---|---|
| READ Retry/Fallback | Primary 内重试两次后才切换 Backup；Plan 输出、Provider history、Trace 与 Events 一致 |
| Minimal Health | UNHEALTHY 高优先级 Provider 被拒绝，HEALTHY Backup 在调用前被选中 |
| WRITE safe fallback | 稳定 idempotency key + 相同 equivalence group 允许 A → B |
| WRITE unsafe fallback | equivalence group 不同返回 `HARNESS.PROVIDER.FALLBACK_UNSAFE`，Backup 零调用 |
| Crash/Resume | SQLite RUNNING checkpoint 固定原 WRITE Provider；重启后即使 Backup 优先级更高也不重新自由选择 |
| Model Fabric | Quality timeout 后切 Backup，并保留 structured output、usage、provider identity 与 MODEL Trace |
| Stage 1 regression | 未声明 ProviderDescriptor 的旧 Echo Plugin 无需修改即可 Direct Invocation |

底层模块测试继续负责完整契约组合，包括 Registry 1:N 冲突、Catalog capability-only、
Selector 稳定键、所有 Retry/Fallback 错误分支、READ/NONE/WRITE Resume 矩阵、Deadline、
取消和 ModelGateway 输入校验。Step 9 的完整回归命令会同时运行这些模块测试、Stage 2
验收和本目录的 Stage 3A Gate。

## Step 7 范围说明

按当前项目决定，Step 7 已缩减为 Provider Observability + Minimal Health。因此本 Gate
把静态 Health、Provider Trace 和 Provider Events 作为阻断项；Provider Pin 外部入口、
Weighted Canary 和 Passive Health 明确暂缓，不计为 Stage 3A 失败。

## 运行

```bash
.venv/bin/python -m pytest tests/stage3a -v
```

完整 Stage 1 / 2 / 3A 回归：

```bash
.venv/bin/python -m pytest \
  harness-contracts/tests harness-spi/tests harness-registry/tests \
  harness-plugin-local/tests harness-selection/tests harness-planning/tests \
  harness-policy/tests harness-model/tests \
  harness-trace/tests harness-runtime/tests harness-state/tests \
  harness-events/tests harness-execution/tests harness-bootstrap/tests \
  plugins/tests tests/stage2 tests/stage3a -v
```

所有 Provider、故障和模型均为确定性本地实现。Restart 测试复制 crash 前已落盘的 SQLite
RUNNING snapshot，再创建新的 Application；旧 task 的取消仅用于资源清理，不作为恢复输入。
