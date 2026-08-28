# plugins

`plugins` 保存具体 Agent/Tool 实现。Harness Core 不导入这里的代码；插件只依赖
`harness-contracts` 和 `harness-spi`，通过 `PluginSPI` 暴露 Capability。Stage 2
ExecutionPlan 使用同一批 Capability 验证多节点执行，插件本身无需感知 DAG、Retry、
Policy、Trace、Checkpoint 或 Resume。

## 示例插件

| 包 | Plugin 类 | Provider | Capability |
|---|---|---|---|
| `echo-agent` | `EchoAgentPlugin` | `EchoAgent` | `echo.reply/v1` |
| `calculator-tool` | `CalculatorToolPlugin` | `CalculatorTool` | `math.calculate/v1` |
| `mock-finance-agent` | `MockFinanceAgentPlugin` | `MockFinanceAgent` | `finance.mock-query/v1` |

三个插件均有稳定 Manifest、Provider 集合和幂等 `initialize()/shutdown()`。安装项目后，
它们通过 `pyproject.toml` 中的 `financeclaw.plugins` entry point 被
`LocalPluginProvider` 自动发现。

测试或嵌入场景可以显式组装：

```python
from calculator_tool import CalculatorToolPlugin
from echo_agent import EchoAgentPlugin
from harness_bootstrap import build_harness

app = build_harness(
    plugins=(EchoAgentPlugin(), CalculatorToolPlugin()),
    entry_point_group=None,
)
```

## Stage 2 / 3A / 3B 用途

- Direct Invocation 回归确保阶段一 API 不被破坏。
- Stage 3A Loader 为旧插件生成稳定 Provider ID；示例插件无需修改即可进入多 Provider
  Registry，并由 Acceptance Gate 验证兼容性。
- Stage 3B FAST/PLAN 统一入口仍通过 CapabilityInvoker 执行这些能力；Router/Planner 只看
  Capability Descriptor，不接触插件或 Provider instance。
- `finance-review-plan` 验收组合 Mock Finance Agent、Calculator Tool、Approval 和
  Echo Agent，验证并行、Join、重启和 Resume。
- Descriptor 的 `execution_profile` 使用安全默认值
  `side_effect=none/egress=none/idempotency=none`；如真实插件具有读写、外连或幂等
  语义，必须显式声明，以便 Scheduler 和 Policy 正确判断。

## 插件开发约束

- 只导入 `harness_contracts` 和 `harness_spi`。
- 一个 Plugin 可以包含一个或多个 Provider，每个 Provider 归属于一个 Plugin。
- 每个 Provider 必须有唯一、稳定的 Capability ID。
- Manifest Capability 集合必须与 Provider Descriptor 完全一致。
- Provider 必须恰好实现 AgentSPI 或 ToolSPI 之一，并返回 `ResultEnvelope`。
- 预期业务错误使用结构化 `HarnessError` 子类。
- 不在插件中复制权限、Registry、Trace、Retry、State 或生命周期编排。

## 测试

```bash
.venv/bin/python -m pytest plugins/tests -v
```
