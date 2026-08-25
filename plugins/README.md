# plugins

`plugins` 保存阶段一的具体 Agent/Tool 实现。Harness Core 不导入这里的实现；插件只依赖 `harness-contracts` 和 `harness-spi`，通过 `PluginSPI` 暴露 Capability。

## 示例插件

| 包 | Plugin 类 | Provider | Capability |
|---|---|---|---|
| `echo-agent` | `EchoAgentPlugin` | `EchoAgent` | `echo.reply/v1` |
| `calculator-tool` | `CalculatorToolPlugin` | `CalculatorTool` | `math.calculate/v1` |
| `mock-finance-agent` | `MockFinanceAgentPlugin` | `MockFinanceAgent` | `finance.mock-query/v1` |

三个插件均具有稳定 Manifest、稳定 Provider 集合和幂等 `initialize()` / `shutdown()`。项目安装后，它们通过 `pyproject.toml` 中的 `financeclaw.plugins` entry point 被 `LocalPluginProvider` 自动发现。

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

## 插件开发约束

- 只导入 `harness_contracts` 和 `harness_spi`。
- 一个 Plugin 可以包含一个或多个 Provider；阶段一中每个 Provider 归属于一个 Plugin。
- 每个 Provider 必须有唯一、稳定的 Capability ID。
- Manifest 的 Capability ID 必须与 Provider Descriptor 完全一致。
- Agent/Tool 必须返回 `ResultEnvelope`，预期业务错误使用结构化 `HarnessError` 子类。
- 不把权限、Registry、Trace 主链或生命周期编排复制到插件中。

## 测试

项目安装后运行：

```bash
.venv/bin/python -m unittest discover -s plugins/tests -v
```
