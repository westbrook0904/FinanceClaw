# FinanceClaw

FinanceClaw 第二阶段（Reliable Plan Execution Engine）已经完成，Stage 3A Provider
Fabric 已实现多 Provider Registry、Health-aware Selection、Retry/Fallback、Provider-safe
Checkpoint/Resume、Provider Observability，以及独立的 ModelProvider/ModelGateway。当前
仓库已接入 Stage 3B 确定性 `handle()` FAST 路径，同时保留阶段一 Direct Invocation API，
并可可靠执行调用方提供的结构化 `ExecutionPlan`。

财经能力仍然只是插件；Harness Core 不包含财经类型、Prompt、SQL、行情访问或其他具体
业务实现。

## 环境与安装

- Python 3.14.3（由 `.python-version` 固定）
- Pydantic v2 公共协议
- FastAPI / Uvicorn 开发依赖
- 所有 Python 包采用 `src` layout，并包含 `py.typed`

```bash
.venv/bin/pip install -e ".[dev]"
```

文档目录名使用连字符，例如 `harness-contracts`；Python 导入名使用下划线，例如
`harness_contracts`。

## 阶段二执行链

```text
Request + ExecutionPlan
        ↓
InvocationLifecycle / PRE_PLAN Policy / PlanValidator
        ↓
ExecutionEngine / BasicScheduler
        ↓
0..N Plan Nodes（serial / parallel / join / branch）
        ↓
CapabilityInvoker
        ↓
Registry → PRE_EXECUTE Policy → Agent / Tool
        ↓
ResultEnvelope + StateStore Checkpoint + Trace + Execution Events
```

`HarnessRuntime.invoke()` 继续提供单 Capability 的 Direct Invocation；Plan 执行使用
`HarnessApplication.execute_plan()`，两条路径共享 `CapabilityInvoker`，不会绕过 Registry、
Policy、Trace、Timeout 或错误归一化边界。

上层应用可使用 `HarnessApplication.handle()` 统一完成模式归一化、PRE_ROUTE、RuleRouter
决策校验和 FAST 调用；一次 handle 只创建一个 InvocationContext、Deadline 和 REQUEST
Trace。PLAN 路径将在 Stage 3B 后续步骤接入。

模型调用使用独立边界，不经过 `CapabilityInvoker`：

```text
Router / Planner / Explorer（Stage 3B）
        ↓
ModelGateway
        ↓
Registry → Selection / Health → Retry / Fallback → ModelProvider
        ↓
GenerateResult + Usage + Provider Identity + Trace / Events
```

## 模块

| 模块 | 职责 |
|---|---|
| `harness-contracts` | Request、Plan、状态、审批、Continuation、Result、能力执行画像与持久化协议 |
| `harness-spi` | 业务无关的 Agent、Tool、Plugin 扩展接口 |
| `harness-registry` | 单 Capability 多 Provider 注册/解析，以及不暴露 Provider instance 的只读 Catalog |
| `harness-routing` | Router SPI、受限 Request 投影、确定性 RuleRouter 与路由决策校验 |
| `harness-selection` | Eligibility、最小 Health 和确定性 Priority Selection |
| `harness-plugin-local` | 本地集合/entry point 发现、插件生命周期和事务回滚 |
| `harness-planning` | Planner SPI/Registry、Static/Hybrid 策略及 Plan 可执行性校验 |
| `harness-policy` | `PRE_ROUTE` / `PRE_PLAN` / `PRE_EXECUTE` 策略链与类型化路由约束 |
| `harness-runtime` | Direct Invocation 与 Plan 共用的受控 Capability 调用边界 |
| `harness-model` | 模型原生协议、ModelProvider SPI、ModelGateway 与确定性 Mock Models |
| `harness-execution` | DAG 调度、重试、取消、Checkpoint/Resume、Approval、Async completion 和结果组合 |
| `harness-state` | `StateStore` SPI、内存快照与 SQLite JSON Snapshot 持久化 |
| `harness-trace` | Request/Plan/Node/Capability Span 与状态事件 |
| `harness-events` | 最小进程内执行事件协议、发布/订阅和内存事件总线 |
| `harness-bootstrap` | 默认依赖组装、应用 API 与插件生命周期 |
| `plugins/*` | Echo Agent、Calculator Tool 和模拟财经 Agent |
| `tests/stage2` | 第二阶段端到端、故障注入与跨进程恢复验收 |
| `tests/stage3a` | Provider Fabric、WRITE safety、Provider Resume 与 ModelGateway 阻断验收 |

## Direct Invocation

默认通过 `financeclaw.plugins` entry point 自动发现三个示例插件：

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
        result = await app.handle(request)
        print(result.model_dump(mode="json"))


asyncio.run(main())
```

Plan 请求允许 `Request.target=None`，但 Direct Invocation 仍要求明确 target，否则返回
`HARNESS.REQUEST.TARGET_REQUIRED`。

需要维持原低层入口语义时仍可调用 `app.invoke(request)`；`handle(request, mode="fast")`
会把 mode sugar 归一化到 Request 副本，不修改调用方原对象。

## Plan 执行与恢复

`ExecutionPlan` 由调用方构造并通过独立入口执行：

```python
async with build_harness() as app:
    result = await app.execute_plan(request, plan)
```

默认 `InMemoryStateStore` 不产生磁盘副作用。需要进程重启后继续相同 `plan_id` 时，注入
`SQLiteStateStore`，重建 Application 后调用 `resume_plan(plan_id)`。到达 Approval 或返回
异步 `ACCEPTED` 的 Capability 时，API 会立即返回带 `Continuation` 的 `ACCEPTED`，外部
系统随后分别通过 `resolve_approval(...)` 或 `complete_async_node(...)` 持久化终态并继续
同一 DAG。

恢复会复用已完成节点和 Provider attempt 结果；已选择 Provider 的 `NONE` / `READ` 节点
优先重放原 Provider，`WRITE` 节点只有在 Capability 声明支持幂等且节点提供稳定
`idempotency_key` 时才固定重放原 Provider。跨 Provider WRITE fallback 还要求相同的非空
`equivalence_group`。Request、Plan、Node 的 Deadline 不会因 Resume 或 Retry 被重置。

## 示例能力

| 插件 | Capability | 类型 | 行为 |
|---|---|---|---|
| `echo-agent` | `echo.reply/v1` | Agent | 原样回显输入 |
| `calculator-tool` | `math.calculate/v1` | Tool | 确定性四则运算 |
| `mock-finance-agent` | `finance.mock-query/v1` | Agent | 返回明确标记的模拟财经结果 |

## HTTP 入口

`main.py` 仍是开发期存活检查，只提供 `GET /health`。Direct Invocation 与 Plan Execution
目前通过 Bootstrap Python API 使用，尚未暴露为 HTTP API。

## 测试

完整 Stage 1 / 2 / 3A 回归（模块测试、插件测试和仓库级验收）：

```bash
.venv/bin/python -m pytest \
  harness-contracts/tests harness-spi/tests harness-registry/tests \
  harness-plugin-local/tests harness-selection/tests harness-routing/tests \
  harness-planning/tests \
  harness-policy/tests harness-model/tests \
  harness-trace/tests harness-runtime/tests harness-state/tests \
  harness-events/tests harness-execution/tests harness-bootstrap/tests \
  plugins/tests tests/stage2 tests/stage3a -v
```

只运行第二阶段仓库级验收：

```bash
.venv/bin/python -m pytest tests/stage2 -v
```

只运行 Stage 3A Provider Fabric 验收：

```bash
.venv/bin/python -m pytest tests/stage3a -v
```

## 架构红线与当前边界

- Harness Core 不导入 `plugins.*` 或任何财经业务实现。
- 业务插件只依赖 `harness-contracts` 和 `harness-spi`。
- Scheduler 不直接访问 Provider；所有 Capability 节点都通过 `CapabilityInvoker`。
- 模型生成只通过 `ModelGateway`；它复用 Provider Fabric，但不经过
  `CapabilityInvoker`，也不作为 DAG Agent/Tool 节点直接执行。
- StateStore 是恢复事实来源；Execution Events 是 best-effort 观察面，不替代 Checkpoint。
- Registry 支持单 Capability 多 Provider，并通过最小 Health-aware PrioritySelector 选择；
  Provider Pin 外部入口、Weighted Canary 和 Passive Health 暂缓。
- 当前 `handle()` 只分派经过独立校验的 FAST Decision；本地 Planner Foundation 已就绪，
  但 PLAN 尚未接入执行。LLM Router、LLM Planner、动态 Plan Patch、
  远程插件、MCP、分布式 Scheduler/锁和外部 Event Broker 尚未实现。
