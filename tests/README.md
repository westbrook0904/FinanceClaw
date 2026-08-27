# tests

测试体系覆盖阶段一 Direct Invocation 兼容性、第二阶段 Reliable Plan Execution Engine
和 Stage 3A Provider Fabric。模块测试与源码模块同目录；顶层 `tests/stage2` 与
`tests/stage3a` 分别提供 Reliable Execution 和 Provider Fabric 仓库级阻断验收。

## 测试位置

| 范围 | 目录 | 重点 |
|---|---|---|
| Contracts | `harness-contracts/tests` | Plan/State/Approval/Result 构造、冻结、校验和 JSON round-trip |
| SPI | `harness-spi/tests` | Agent/Tool 语义分离和 Manifest 一致性 |
| Registry | `harness-registry/tests` | 注册、过滤、唯一解析、所有权和只读 Catalog |
| Selection | `harness-selection/tests` | Eligibility、Health 排序、拒绝原因和稳定 Selection |
| Local Plugin | `harness-plugin-local/tests` | 发现、生命周期和事务回滚 |
| Planning | `harness-planning/tests` | DAG、引用、Binding、Condition 与 Capability 可执行性 |
| Policy | `harness-policy/tests` | PRE_PLAN/PRE_EXECUTE、决策聚合与 Approval |
| Trace | `harness-trace/tests` | Span 生命周期、层级、续接和 Console 输出 |
| Runtime | `harness-runtime/tests` | Direct Invocation、Invoker、timeout、取消和错误归一化 |
| Model | `harness-model/tests` | Quality Selection、timeout、Retry/Fallback、structured output、usage 和 trace |
| State | `harness-state/tests` | 内存/SQLite Snapshot、错误与重建加载 |
| Events | `harness-events/tests` | 内存总线、订阅与 NoOp Publisher |
| Execution | `harness-execution/tests` | DAG、Retry、Resume、Approval、Async、Trace 和 Events |
| Bootstrap | `harness-bootstrap/tests` | 依赖组装、Application API 和生命周期 |
| Plugins | `plugins/tests` | 三个示例插件行为、打包和集成 |
| Stage 2 acceptance | `tests/stage2` | E2E、fault injection、SQLite restart 与 fail-closed |
| Stage 3A acceptance | `tests/stage3a` | Multi-provider E2E、WRITE safety、Provider restart、Model Fabric 与旧插件回归 |

## 运行完整回归

```bash
.venv/bin/python -m pytest \
  harness-contracts/tests harness-spi/tests harness-registry/tests \
  harness-plugin-local/tests harness-selection/tests harness-planning/tests \
  harness-policy/tests harness-model/tests \
  harness-trace/tests harness-runtime/tests harness-state/tests \
  harness-events/tests harness-execution/tests harness-bootstrap/tests \
  plugins/tests tests/stage2 tests/stage3a -v
```

只运行仓库级第二阶段验收：

```bash
.venv/bin/python -m pytest tests/stage2 -v
```

只运行仓库级 Stage 3A 验收：

```bash
.venv/bin/python -m pytest tests/stage3a -v
```

测试不依赖真实网络、行情、LLM 或外部数据库；ModelGateway 使用确定性 Mock Providers，
跨进程语义使用临时 SQLite 文件验证。
