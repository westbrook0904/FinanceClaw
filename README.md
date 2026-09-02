# FinanceClaw

FinanceClaw 正在从自研通用 Agent 框架收敛为“开源运行时 + 金融 Agent 领域核心”。当前
`main` 已移除自研 Router、LLM Planner/PlanDraft、模型协议栈、ReAct 循环、DAG 调度器、
checkpoint/恢复内核；这些通用能力后续分别复用 LangChain 与 LangGraph。

FinanceClaw 自己继续聚焦四件事：

- Agent 的记忆与上下文工程；
- 工具/Capability 的注册、发现、过滤与投影；
- 工具调用前后的 Policy、权限、审批、幂等和 Provider 安全；
- 金融场景需要的稳定结果、错误、Trace 与评测闭环。

## 已确定的顶层路径

```text
Request
  ├─ 显式 Capability  → DIRECT   → CapabilityInvoker
  ├─ 显式 Workflow    → WORKFLOW → Published StateGraph（待实现）
  └─ 未指定目标        → AGENT    → LangChain Agent / LangGraph（待实现）
```

顶层不再让 LLM 选择所谓 FAST/PLAN 模式，也不要求 LLM 一次性生成复杂 PlanDraft。无明确目标
的请求直接进入顶层 Agent；重复且可验证的业务流程发布为固定、版本化 Workflow。

## 当前可用核心

| 模块 | 职责 |
|---|---|
| `harness-contracts` | Request、Capability、Provider、Context、Memory、Result、Error、Retry 等稳定协议 |
| `harness-spi` | Agent、Tool、Plugin 的最小扩展接口 |
| `harness-registry` | Capability 与 Provider 的 1:N 注册及只读 Catalog |
| `harness-selection` | Eligibility、Health 与确定性 Provider 选择 |
| `harness-policy` | Context、Memory 和 Capability 调用治理 |
| `harness-context` | Context Source、投影、裁剪与安全 Prompt 构建基础 |
| `harness-memory` | Memory Provider/Gateway、scope、namespace、TTL、evidence 与 Policy |
| `harness-runtime` | 统一 `CapabilityInvoker` 与 Direct Invocation |
| `harness-plugin-local` | 本地集合与 entry point 插件发现、事务加载和生命周期 |
| `harness-trace` | 领域 Trace/Span 和瞬时事件 |
| `harness-events` | Provider 调用事件发布/订阅边界 |
| `harness-bootstrap` | 上述核心的 Composition Root 与应用生命周期 |
| `plugins/*` | 示例 Agent/Tool Capability；财经能力仍与 Core 解耦 |

当前 Direct Invocation 保留 Provider retry/fallback：READ 可按策略重试并降级；WRITE 只有在
稳定 idempotency key 与相同非空 `equivalence_group` 同时成立时才允许跨 Provider fallback。
这属于 FinanceClaw 的工具执行安全，不由 LangChain 模型重试或 LangGraph 节点重试取代。

## 安装

- Python `>=3.14.3,<3.15`
- Pydantic v2
- 所有包采用 `src` layout 并提供 `py.typed`

```bash
.venv/bin/pip install -e ".[dev]"
```

## Direct Invocation

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

`Request.target=None` 为未来顶层 Agent 入口保留；当前 `app.invoke()` 遇到无目标请求会返回
`HARNESS.REQUEST.TARGET_REQUIRED`。

## 测试

```bash
.venv/bin/python -m pytest \
  harness-contracts/tests harness-spi/tests harness-registry/tests \
  harness-context/tests harness-memory/tests harness-plugin-local/tests \
  harness-selection/tests harness-policy/tests harness-trace/tests \
  harness-runtime/tests harness-events/tests harness-bootstrap/tests \
  plugins/tests tests/stage3a -q

.venv/bin/python -m ruff check \
  harness-bootstrap harness-contracts harness-context harness-events \
  harness-memory harness-plugin-local harness-policy harness-registry \
  harness-runtime harness-selection harness-spi harness-trace plugins tests main.py
```

## 架构决议

当前收敛由以下文档共同定义：

- `.design/FinanceClaw-LemonClaw-架构对齐分析.md`
- `.design/FinanceClaw-顶层Agent与确定性Workflow-ADR讨论稿.md`
- `.design/FinanceClaw-LangChain模型运行时复用-ADR讨论稿.md`
- `.design/FinanceClaw-LangGraph编排运行时复用-ADR讨论稿.md`

下一步不会恢复被删除的通用框架代码，而是设计三个薄适配面：`ModelRuntime`、
`AgentRuntime/GraphRuntime`、`CapabilityToolAdapter`。框架负责运行机制，FinanceClaw 仍拥有
身份、上下文、记忆、工具授权和调用安全。
