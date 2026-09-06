"""真实图预算、重试、资源并发与子上下文隔离回归。"""

import asyncio
from threading import BoundedSemaphore
from types import SimpleNamespace
from typing import ClassVar

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from financeclaw.application.execution_service import agent_snapshot
from financeclaw.kernel import ExecutionContext
from financeclaw.modules.execution import ExecutionConflict
from financeclaw.orchestration.agents import OfflineFinanceModel
from financeclaw.orchestration.agents.execution_middleware import ExecutionBudgetMiddleware
from financeclaw.orchestration.tools import MarketSnapshotTool
from tests.stage1.test_agent import components_with_tools, context
from tests.stage6fix.test_batch_tools import BatchModel, call
from tests.stage6fix.test_execution_recovery import OWNER, stack


@pytest.mark.asyncio
async def test_retries_and_children_share_persistent_root_budget(tmp_path):
    """A09：原生重试的每一次调用均计数，子任务和新尝试不能刷新根预算。"""
    persisted, _, _, service = stack(tmp_path)
    market = MarketSnapshotTool(fail_first=1)
    components, _ = components_with_tools(market=market)
    components.agent_factory.conversation_repository = SimpleNamespace(execution=service.execution)
    profile = components.default_agent_profile.model_copy(update={"max_tree_tool_calls": 2})
    root = context("market:read").model_copy(update={"root_run_id": "run-agent"})
    snapshot = agent_snapshot(profile, root, thread_id="budget", input_hash="hash")
    service.execution.register(root.run_id, snapshot)
    graph = components.agent_factory.build(profile, model=OfflineFinanceModel())
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "read AAPL"}]},
        config={"configurable": {"thread_id": "budget"}},
        context=root,
    )
    assert result["messages"][-1].content and market.call_count == 2
    assert service.execution.get(root.run_id)["tool_calls"] == 2
    child = root.model_copy(update={"run_id": "child", "parent_run_id": root.run_id})
    service.execution.register(
        child.run_id,
        agent_snapshot(profile, child, thread_id="child", input_hash="other"),
        root_run_id=root.run_id,
    )
    with pytest.raises(ExecutionConflict, match="budget exhausted"):
        service.execution.consume(child.run_id, "tool")
    assert service.execution.get(root.run_id)["tool_calls"] == 2
    persisted.database.close()


@pytest.mark.asyncio
async def test_atomic_budget_and_operation_claim_across_repository_instances(tmp_path):
    """CAS 在共享数据库上仲裁，不依赖单服务内存锁。"""
    components, _, _, service = stack(tmp_path)
    root = ExecutionContext(**OWNER, run_id="root", root_run_id="root", turn_id="t")
    snapshot = agent_snapshot(
        components.default_agent_profile, root, thread_id="t", input_hash="hash"
    )
    snapshot["limits"]["tool"] = 7
    service.execution.register(root.run_id, snapshot)

    async def consume():
        """每个调用建立独立仓储实例，模拟不同 worker。"""
        repository = type(service.execution)(components.database.session_factory)
        try:
            await asyncio.to_thread(repository.consume, root.run_id, "tool")
            return True
        except ExecutionConflict:
            return False

    assert sum(await asyncio.gather(*(consume() for _ in range(30)))) == 7
    service.execution.prepare(
        "one-operation", root.run_id, {"command": {"resume": {"decisions": [{"type": "reject"}]}}}
    )
    assert (
        sum(
            await asyncio.gather(
                *(asyncio.to_thread(service.execution.claim, "one-operation") for _ in range(20))
            )
        )
        == 1
    )
    assert service.execution.get(root.run_id)["operation_calls"] == 1
    assert service.execution.get(root.run_id)["side_effects_denied"]
    service.execution.request_cancel(root.run_id)
    with pytest.raises(ExecutionConflict, match="cancellation"):
        service.execution.consume(root.run_id, "model")
    components.database.close()


class CaptureTaskModel(OfflineFinanceModel):
    """截取实际下发模型的完整消息；不执行任何工具。"""

    seen: ClassVar[list] = []

    def _generate(self, messages, *args, **kwargs):
        """记录 LangChain 最终消息，检验真正的装配隔离而非只看配置字段。"""
        self.seen.extend(messages)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="task-only result"))]
        )


def test_child_factory_never_loads_root_journal_or_memory(tmp_path):
    """B01：保留 conversation 身份审计，但不调用根上下文或记忆服务。"""
    components, _, _, _ = stack(tmp_path)

    def forbidden(*args, **kwargs):
        """任何对子任务隐式加载根历史的尝试都应令测试失败。"""
        raise AssertionError("root history was accessed by a delegated Agent")

    components.agent_factory.context_builder = SimpleNamespace(build=forbidden)
    components.agent_factory.memory_service = SimpleNamespace(search=forbidden)
    profile = components.agent_profiles.resolve("market_research_agent").model_copy(
        update={"output_schema": None}
    )
    CaptureTaskModel.seen.clear()
    graph = components.agent_factory.build(profile, model=CaptureTaskModel())
    child = context("market:read").model_copy(
        update={"conversation_id": "parent-conversation", "delegation_id": "child"}
    )
    graph.invoke(
        {"messages": [{"role": "user", "content": "only this authorized task"}]},
        config={"configurable": {"thread_id": "isolated-child"}},
        context=child,
    )
    assert any(m.content == "only this authorized task" for m in CaptureTaskModel.seen)
    assert not any("financeclaw_stable_memory" in str(m.content) for m in CaptureTaskModel.seen)
    components.database.close()


@pytest.mark.asyncio
async def test_worker_resource_gate_limits_and_releases_on_error():
    """原生并发工具仍受资源门控，异常与取消不泄漏 semaphore。"""
    components, _ = components_with_tools()
    middleware = ExecutionBudgetMiddleware(None, components.tool_catalog, BoundedSemaphore(2))
    request = SimpleNamespace(runtime=SimpleNamespace(context=context("tools:read")))
    active = maximum = 0

    async def handler(_):
        """用短异步 I/O 观察并发峰值，并注入一个可恢复错误。"""
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.01)
            raise ValueError("tool failure")
        finally:
            active -= 1

    results = await asyncio.gather(
        *(middleware.awrap_tool_call(request, handler) for _ in range(8)), return_exceptions=True
    )
    assert maximum == 2 and all(isinstance(item, ValueError) for item in results)
    assert middleware.gate.acquire(blocking=False) and middleware.gate.acquire(blocking=False)
    middleware.gate.release()
    middleware.gate.release()


def test_batch_limit_and_rejection_do_not_reach_hitl(tmp_path):
    """超限 READ 与用户拒绝后的写动作都在审批和执行之前阻止。"""
    components, _, _, service = stack(tmp_path)
    profile = components.default_agent_profile.model_copy(
        update={"max_tool_batch": 1, "context_policy": "delegated-task-only-v1"}
    )
    root = context("tools:read", "watchlist:write").model_copy(update={"root_run_id": "run-agent"})
    service.execution.register(
        root.run_id, agent_snapshot(profile, root, thread_id="guard", input_hash="hash")
    )
    calls = [call("calculate", i, operation="add", left=i, right=1) for i in (1, 2)]
    graph = components.agent_factory.build(profile, model=BatchModel(calls=calls))
    result = graph.invoke(
        {"messages": [{"role": "user", "content": "do it"}]},
        config={"configurable": {"thread_id": "guard"}},
        context=root,
    )
    assert "configured limit" in result["messages"][-1].content
    service.execution.deny_side_effects(root.run_id)
    graph = components.agent_factory.build(
        profile, model=BatchModel(calls=[call("watchlist_add", 1, symbol="AAPL", note="refused")])
    )
    result = graph.invoke(
        {"messages": [{"role": "user", "content": "continue"}]},
        config={"configurable": {"thread_id": "denied"}},
        context=root,
    )
    assert "user rejected" in result["messages"][-1].content and not result.get("__interrupt__")
    assert service.execution.get(root.run_id)["tool_calls"] == 0
    components.database.close()
