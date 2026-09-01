# FinanceClaw

FinanceClaw 第二阶段（Reliable Plan Execution Engine）、Stage 3A Provider Fabric 与
Stage 3B Routing & Planning 已完成。当前仓库具备多 Provider Registry、Health-aware
Selection、Retry/Fallback、Provider-safe Checkpoint/Resume、ModelGateway，以及统一的
`handle()` AUTO / FAST / PLAN 路径；阶段一 Direct Invocation API 和阶段二低层 Plan API
继续保持兼容。

Stage 3C 的 Plan Identity、Strict Structured Output、Agent Foundation F1 Routing
correctness、F2 Context Engineering、F3 Memory 与 F4b Minimal Explore Loop 已实现。
F5 Real-use Gate 的官方 OpenAI Python SDK Responses Provider adapter、组合风险 Agent、脱敏评测报告与
显式 live runner 已就绪；当前环境缺少真实 API 凭证，因此一期 Gate 尚未宣告通过。
`HYBRID`、PlanPatch 和高阶资源预算继续延后。F4b 已把
Profile/Turn/Action/Observation/Checkpoint 契约和单节点 wrapper 接入 standalone
`EXPLORE`；未配置 Profile/单写者保证时 EXPLORE 仍 fail-closed，HYBRID 始终不可用。当前优先级见
`.design/FinanceClaw-Agent-Foundation-一期路线图.md`，当前 Context、Memory 与 Minimal Explore
契约见 `.design/FinanceClaw-Agent-Foundation-一期实施说明书.md`。旧 Stage 3C 完整编排文档已
整体降级为高阶设计储备。

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
决策校验与 FAST / PLAN 分派；PLAN 由服务端配置和 Policy 选择 Planner，再通过
ExecutionEngine 的 context-aware 入口执行。一次 handle 只创建一个 InvocationContext、
Deadline 和 REQUEST Trace。

模型调用使用独立边界，不经过 `CapabilityInvoker`：

```text
Router / Planner（已实现；Explorer 按 Agent Foundation 实施）
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
| `harness-contracts` | Request、Plan、Context、Memory、Explore、状态、审批、Continuation、Result 与执行画像协议 |
| `harness-context` | ContextSource、Assembler、PRE_CONTEXT Policy、Snapshot、Projection 与安全 Prompt Builder |
| `harness-memory` | MemoryProvider/Gateway、InMemory/SQLite、Policy、scope/namespace、TTL 与有界检索 |
| `harness-agentic` | Minimal Explore Profile eligibility、standalone wrapper、strict turn loop、scoped Action、Observation 与安全恢复 |
| `harness-spi` | 业务无关的 Agent、Tool、Plugin 扩展接口 |
| `harness-registry` | 单 Capability 多 Provider 注册/解析，以及不暴露 Provider instance 的只读 Catalog |
| `harness-routing` | deterministic-first RoutingPipeline、安全 Request 投影、Rule/LLM Router 与决策校验 |
| `harness-selection` | Eligibility、最小 Health 和确定性 Priority Selection |
| `harness-plugin-local` | 本地集合/entry point 发现、插件生命周期和事务回滚 |
| `harness-planning` | Planner SPI/Registry、Static/Hybrid/LLM 策略、PlanTemplate/Materializer、PlanDraft 及可执行性校验 |
| `harness-policy` | Context / Memory / Route / Plan / Execute 的类型化策略链与约束 |
| `harness-runtime` | Direct Invocation 与 Plan 共用的受控 Capability 调用边界 |
| `harness-model` | 模型原生协议、strict structured output、两阶段 reservation、ModelGateway 与确定性 Mock Models |
| `harness-execution` | DAG 调度、重试、取消、Checkpoint/Resume、Approval、Async completion 和结果组合 |
| `harness-state` | `StateStore` SPI、内存快照与 SQLite JSON Snapshot 持久化 |
| `harness-trace` | Request/Route/Planner/Model/Plan/Node/Provider Span 与瞬时事件 |
| `harness-events` | 最小进程内执行事件协议、发布/订阅和内存事件总线 |
| `harness-bootstrap` | 默认依赖组装、应用 API 与插件生命周期 |
| `plugins/*` | Echo Agent、Calculator Tool、模拟财经 Agent 与真实组合风险检查 Agent |
| `real-use` | F5 真实 Provider + 财经场景组合、SQLite 证据与脱敏 Gate 报告 |
| `tests/stage2` | 第二阶段端到端、故障注入与跨进程恢复验收 |
| `tests/stage3a` | Provider Fabric、WRITE safety、Provider Resume 与 ModelGateway 阻断验收 |
| `tests/stage3b` | ExecutionMode、Rule/LLM Route、LLM Plan/Repair、Policy、Lifecycle 与回归 Gate |
| `tests/stage3c` | Agent Foundation 验收索引；F0–F4b 与 F5 Gate wiring 由其及相关模块回归共同阻断 |

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

`execute_plan()` 是使用具体 `plan_id/revision` 的 advanced API，不重新分配身份；同一
`plan_id` 重复创建返回 `HARNESS.PLAN.EXECUTION_ID_CONFLICT`。标准 `handle()` PLAN 路径把
Planner 输出视为 candidate，经 `PlannerOutputNormalizer -> PlanMaterializer` 每次生成新的
`plan_id`，并从 `revision=1` 开始。

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

完整 Stage 1 / 2 / 3A / 3B 与 Agent Foundation 当前前置步骤回归（模块测试、插件测试和仓库级验收）：

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

只运行第二阶段仓库级验收：

```bash
.venv/bin/python -m pytest tests/stage2 -v
```

只运行 Stage 3A Provider Fabric 验收：

```bash
.venv/bin/python -m pytest tests/stage3a -v
```

只运行 Stage 3B Routing & Planning 验收：

```bash
.venv/bin/python -m pytest tests/stage3b -v
```

## 架构红线与当前边界

- Harness Core 不导入 `plugins.*` 或任何财经业务实现。
- 业务插件只依赖 `harness-contracts` 和 `harness-spi`。
- Scheduler 不直接访问 Provider；所有 Capability 节点都通过 `CapabilityInvoker`。
- 模型生成只通过 `ModelGateway`；它复用 Provider Fabric，但不经过
  `CapabilityInvoker`，也不作为 DAG Agent/Tool 节点直接执行。
- StateStore 是恢复事实来源；Execution Events 是 best-effort 观察面，不替代 Checkpoint。
- MemoryProvider 只保存获准跨请求复用的事实，不保存执行状态；公开读写必须经过
  MemoryGateway 的可信 scope、namespace、evidence、Policy、TTL 与大小治理。Router/Planner
  只从 ContextProjection 消费 DATA tier Memory，不直接查询 Provider。
- Registry 支持单 Capability 多 Provider，并通过最小 Health-aware PrioritySelector 选择；
  Provider Pin 外部入口、Weighted Canary 和 Passive Health 暂缓。
- `handle()` 分派经过独立校验的 FAST / PLAN Decision。Router 产出经验证的
  `RouteDecision`（mode / route type / direct capability），不选择 Provider；服务端配置和
  Policy 选择 Planner。RoutingPipeline 先做 deterministic 匹配，只有类型化
  `HARNESS.ROUTE.NO_MATCH` 才进入可选模型 fallback；模型只补全未知路由字段。LLMPlanner
  可自主生成受限 PlanDraft、
  执行 bounded repair，再经 Coordinator/PlanMaterializer 统一物化和
  ExecutionEngine 二次验证后执行。
  WAITING / crash resume 复用持久化 Plan，不重新路由或规划。动态 Plan Patch、远程插件、
  MCP、分布式 Scheduler/锁和外部 Event Broker 尚未实现。
- `EXPLORE` 仅在 Composition Root 同时配置可信 Profile 和
  `single_writer_guaranteed=True` 时开放 standalone 单节点 loop；未配置时继续 fail-closed。
  Action 仅允许 `NONE/READ + NONE/INTERNAL + SYNC`，Approval/Async 不进入等待状态。
  `HYBRID`、PlanPatch 与完整 Replay Eval 均需在一期真实使用后重新评审。
- Route / Planner 已接入同一 handle trace：ROUTE 包含安全的 RequestSummary/Catalog hash，
  ROUTE / PLANNER 同时记录 Context snapshot/projection hash 与 item 数量，PLANNER 记录 attempt
  与验证摘要，repair 使用瞬时 Event 表示；观察面不保存 Context 原文、完整输入、Prompt、模型响应、
  Provider 原始错误消息或隐藏推理。
