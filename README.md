# FinanceClaw

FinanceClaw 第一阶段已经完成：当前仓库提供一个最小、边界清晰的 Harness Runtime，能够接收标准 Request，构造可信 Context，发现并注册本地 Agent/Tool 插件，执行 Policy，调用目标 Capability，并生成完整 Trace。

财经能力只是插件；Harness Core 不包含财经类型、Prompt、SQL、行情访问或其他具体业务实现。

## 环境与安装

- Python 3.14.3（由 `.python-version` 固定）
- Pydantic v2 公共协议
- FastAPI / Uvicorn 开发依赖
- 所有 Python 包采用 `src` layout，并包含 `py.typed`

```bash
.venv/bin/pip install -e ".[dev]"
```

文档目录名使用连字符，例如 `harness-contracts`；Python 导入名使用下划线，例如 `harness_contracts`。

## 阶段一调用链

```text
Request
  ↓
InvocationContextFactory
  ↓
Trace: REQUEST / RUNTIME
  ↓
CapabilityRegistry.resolve
  ↓
PolicyEngine: PRE_EXECUTE
  ↓
AgentSPI.invoke / ToolSPI.execute
  ↓
ResultEnvelope
  ↓
Trace finished
```

`harness-bootstrap` 是唯一的 Composition Root，负责创建并共享 Registry、Policy、Trace、Plugin Loader、ContextFactory 和 Runtime 实例。

## 模块

| 模块 | 阶段一实现 |
|---|---|
| `harness-contracts` | Request、Context、Capability、Result 和 Error 稳定协议 |
| `harness-spi` | 语义独立的 Agent、Tool、Plugin 扩展接口 |
| `harness-registry` | 线程安全的内存能力注册、过滤和唯一解析 |
| `harness-plugin-local` | 显式集合/entry point 发现、插件生命周期和失败回滚 |
| `harness-planning` | ExecutionPlan 结构、引用、DAG 与 Capability 可执行性校验 |
| `harness-policy` | PRE_EXECUTE 策略链、拒绝短路和约束合并 |
| `harness-trace` | Tracer SPI、内存 Trace 和 JSON Lines Console Trace |
| `harness-runtime` | 单次 Invocation 的薄协调层、超时和错误归一化 |
| `harness-bootstrap` | 默认依赖组装与应用启动/关闭生命周期 |
| `plugins/*` | Echo Agent、Calculator Tool 和模拟财经 Agent |
| `tests/*` | 契约、模块、插件和完整主链验证说明 |

## 最小示例

项目安装后，默认通过 `financeclaw.plugins` entry point 自动发现三个示例插件：

```python
import asyncio

from harness_bootstrap import build_harness
from harness_contracts import Request, RequestInput, RequestTarget


async def main() -> None:
    request = Request(
        input=RequestInput(type="text", content="hello"),
        target=RequestTarget(capability="echo.reply/v1"),
    )

    async with build_harness() as app:
        result = await app.invoke(request)
        print(result.model_dump(mode="json"))


asyncio.run(main())
```

测试或嵌入场景也可以使用 `build_harness(plugins=(...))` 显式提供插件，并把 `entry_point_group=None` 关闭自动发现。

## 示例能力

| 插件 | Capability | 类型 | 行为 |
|---|---|---|---|
| `echo-agent` | `echo.reply/v1` | Agent | 原样回显输入 |
| `calculator-tool` | `math.calculate/v1` | Tool | 确定性四则运算 |
| `mock-finance-agent` | `finance.mock-query/v1` | Agent | 返回明确标记的模拟财经结果 |

## HTTP 入口

当前 `main.py` 仍是开发期存活检查，只提供 `GET /health`，尚未把 Harness Invocation 暴露为 HTTP API。完整阶段一能力通过 Bootstrap Python API 和测试使用。

## 测试

安装项目后可分别运行所有模块测试：

```bash
for suite in harness-contracts harness-spi harness-registry harness-plugin-local harness-policy harness-trace harness-runtime harness-planning harness-bootstrap plugins; do
  .venv/bin/python -m unittest discover -s "$suite/tests" -v || exit 1
done
```

## 依赖红线

- Harness Core 不导入 `plugins.*` 或任何财经业务实现。
- 业务插件只依赖 `harness-contracts` 和 `harness-spi`。
- Registry、Policy、Trace 彼此不直接协作，由 Runtime 统一协调。
- Plugin Loader 负责“能力从哪里来”，Registry 负责“有哪些能力”。
- Bootstrap 决定实例数量和生命周期；基础设施类不实现全局单例。

## 阶段一非目标

当前不实现 Planner、LLM Router、多 Agent 编排、Memory、RAG、Workflow、远程插件、MCP、HTTP Provider、数据库持久化、OpenTelemetry 后端或热升级。
