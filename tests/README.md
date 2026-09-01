# tests

测试体系覆盖阶段一 Direct Invocation 兼容性、第二阶段 Reliable Plan Execution Engine、
Stage 3A Provider Fabric、Stage 3B Routing & Planning，以及 Agent Foundation F0–F5 Gate
wiring。模块测试
与源码模块同目录；顶层 `tests/stage2`、`tests/stage3a`、`tests/stage3b`、`tests/stage3c`
分别提供跨模块仓库级阻断验收。

## 测试位置

| 范围 | 目录 | 重点 |
|---|---|---|
| Contracts | `harness-contracts/tests` | Plan/State/Approval/Result 构造、冻结、校验和 JSON round-trip |
| Context | `harness-context/tests` | 稳定 hash、Policy-before-Snapshot、trust/redaction、consumer projection 与 deterministic truncation |
| Memory | `harness-memory/tests` | scope/namespace、TTL、Policy、大小、幂等冲突、InMemory/SQLite 与持久化 |
| Agentic | `harness-agentic/tests` | F4 Profile eligibility、canonical hash、standalone loop、scoped Action 与 checkpoint guard |
| SPI | `harness-spi/tests` | Agent/Tool 语义分离和 Manifest 一致性 |
| Registry | `harness-registry/tests` | 注册、过滤、唯一解析、所有权和只读 Catalog |
| Selection | `harness-selection/tests` | Eligibility、Health 排序、拒绝原因和稳定 Selection |
| Local Plugin | `harness-plugin-local/tests` | 发现、生命周期和事务回滚 |
| Routing | `harness-routing/tests` | RoutingPipeline、未知字段补全、安全投影、Rule/LLM Router、Validator 与依赖边界 |
| Planning | `harness-planning/tests` | DAG 校验、Static/Hybrid/LLM Planner、PlanDraft 与 bounded repair |
| Policy | `harness-policy/tests` | Context/Memory/Route/Plan/Execute phase、约束收紧、决策聚合与 Approval |
| Trace | `harness-trace/tests` | Span 生命周期、层级、续接和 Console 输出 |
| Runtime | `harness-runtime/tests` | Direct Invocation、Invoker、timeout、取消和错误归一化 |
| Model | `harness-model/tests` | Quality Selection、timeout、Retry/Fallback、structured output、usage 和 trace |
| State | `harness-state/tests` | 内存/SQLite Snapshot、错误与重建加载 |
| Events | `harness-events/tests` | 内存总线、订阅与 NoOp Publisher |
| Execution | `harness-execution/tests` | DAG、Retry、Resume、Approval、Async、Trace 和 Events |
| Bootstrap | `harness-bootstrap/tests` | 依赖组装、Application API 和生命周期 |
| Plugins | `plugins/tests` | 四个示例/业务插件行为、打包和集成 |
| Stage 2 acceptance | `tests/stage2` | E2E、fault injection、SQLite restart 与 fail-closed |
| Stage 3A acceptance | `tests/stage3a` | Multi-provider E2E、WRITE safety、Provider restart、Model Fabric 与旧插件回归 |
| Stage 3B acceptance | `tests/stage3b` | ExecutionMode、Rule/LLM Route、LLM Plan/Repair、Policy、Lifecycle/Resume 与全阶段兼容性 |
| Foundation acceptance | `tests/stage3c` | F0–F4b 与 F5 adapter/业务/Gate wiring；由相关模块和 Stage 3B 回归共同阻断 |

## 运行完整回归

```bash
.venv/bin/python -m pytest \
  harness-contracts/tests harness-spi/tests harness-registry/tests \
  harness-context/tests harness-memory/tests harness-agentic/tests \
  harness-plugin-local/tests harness-selection/tests harness-routing/tests \
  harness-planning/tests \
  harness-policy/tests harness-model/tests \
  harness-trace/tests harness-runtime/tests harness-state/tests \
  harness-events/tests harness-execution/tests harness-bootstrap/tests \
  plugins/tests tests/stage2 tests/stage3a tests/stage3b tests/stage3c -v
```

只运行仓库级第二阶段验收：

```bash
.venv/bin/python -m pytest tests/stage2 -v
```

只运行仓库级 Stage 3A 验收：

```bash
.venv/bin/python -m pytest tests/stage3a -v
```

只运行仓库级 Stage 3B 验收：

```bash
.venv/bin/python -m pytest tests/stage3b -v
```

只运行仓库级 Agent Foundation 当前前置步骤验收：

```bash
.venv/bin/python -m pytest tests/stage3c -v
```

默认测试不依赖真实网络、行情、LLM 或外部数据库；OpenAI adapter 使用记录型 HTTP transport，
其他 ModelGateway 路径使用确定性 Mock Providers，跨进程语义使用临时 SQLite 文件验证。F5
live Gate 只在显式 `--live` 且配置 API key 后运行，不属于默认 pytest。
