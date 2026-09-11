"""事实、输出预算、错误审计和发布恢复的故障回归。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from financeclaw.agent_server.agents.ziwei_offline import OfflineZiweiModel
from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.agent_server.middleware.artifact_middleware import ToolResultArtifactMiddleware
from financeclaw.agent_server.middleware.middleware import ToolGovernanceMiddleware
from financeclaw.shared.audit.models import AuditEventType
from financeclaw.shared.turns.types import ExecutionConflict
from tests.stage7.support import build_ziwei_agent, components, context, envelope, request
from tests.turn_support import seed_execution
from tests.worker_scope import worker_snapshot


class NoEvidenceModel(OfflineZiweiModel):
    """直接声称完成但没有调用工具的模型。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """自然语言不能成为盘面成功凭证。"""
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="已排好盘"))])


class OversizedInterpretationModel(OfflineZiweiModel):
    """自由文本整体过大，仍必须在 child 交付前拒绝。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """只有最终文本变大，取证保持不变。"""
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        if not self._bound_tool_names:
            result.generations[0].message.content = "文" * 12_000
        return result


@pytest.mark.asyncio
async def test_oversized_final_result_fails_before_parent_delivery():
    """只检查盘面字节数不够，带解读的完整结果也有原子交付上限。"""
    stack = components()
    graph = build_ziwei_agent(
        stack.agent_factory,
        stack.agent_profiles.resolve("ziwei_doushu_agent"),
        stack.ziwei_service,
        model=OversizedInterpretationModel(),
    )
    with pytest.raises(ZiweiError) as error:
        await graph.ainvoke(envelope(request(mode="interpretation")), context=context())
    assert error.value.code == "ZIWEI_CONTEXT_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_absent_chart_fails():
    """模型没有调用排盘工具时，正文不能冒充真实盘面。"""
    stack = components()
    graph = build_ziwei_agent(
        stack.agent_factory,
        stack.agent_profiles.resolve("ziwei_doushu_agent", "2.2.0"),
        stack.ziwei_service,
        model=NoEvidenceModel(),
    )
    with pytest.raises(ZiweiError) as error:
        await graph.ainvoke(envelope(request(mode="interpretation")), context=context())
    assert error.value.code == "ZIWEI_RESULT_INVALID"


@pytest.mark.asyncio
async def test_finalization_cannot_bypass_persistent_root_budget(tmp_path):
    """根总预算为 2 时，仅够取证的两个模型轮次，finalization 必须拒绝。"""
    stack = components(tmp_path)
    profile = stack.agent_profiles.resolve("ziwei_doushu_agent")
    owner = context(turn_id="ziwei-child")
    snapshot = worker_snapshot(
        profile,
        owner,
        stack.tool_catalog,
        stack.model_profiles,
        thread_id="budget-child",
        input_hash="synthetic",
    )
    snapshot["limits"]["model"] = 2
    owner = seed_execution(stack.conversation_repository.execution, owner, snapshot)
    try:
        graph = build_ziwei_agent(
            stack.agent_factory, profile, stack.ziwei_service, model=OfflineZiweiModel()
        )
        with pytest.raises(ExecutionConflict, match="budget"):
            await graph.ainvoke(envelope(request(mode="interpretation")), context=owner)
        assert stack.conversation_repository.execution.get(owner.turn_id)["model_calls"] == 2
    finally:
        stack.database.close()


@pytest.mark.asyncio
async def test_initialize_rejects_release_drift_even_for_clarification(tmp_path):
    """没有模型调用的澄清分支也不能用不同发布恢复旧任务。"""
    stack = components(tmp_path)
    profile = stack.agent_profiles.resolve("ziwei_doushu_agent")
    owner = context(turn_id="ziwei-child")
    snapshot = worker_snapshot(
        profile,
        owner,
        stack.tool_catalog,
        stack.model_profiles,
        thread_id="pinned-child",
        input_hash="synthetic",
    )
    owner = seed_execution(stack.conversation_repository.execution, owner, snapshot)
    drifted = profile.model_copy(update={"configuration_fingerprint": "different-release"})
    try:
        graph = build_ziwei_agent(
            stack.agent_factory, drifted, stack.ziwei_service, model=OfflineZiweiModel()
        )
        with pytest.raises(ExecutionConflict, match="pinned"):
            await graph.ainvoke(envelope(request(birth={})), context=owner)
        assert stack.conversation_repository.execution.get(owner.turn_id)["model_calls"] == 0
    finally:
        stack.database.close()


def test_protected_results_are_never_offloaded_or_token_truncated(tmp_path):
    """跨 child→root 也保留结构，不能返回截断摘要假装完整证据。"""
    stack = components(tmp_path)
    stack.artifact_service.inline_bytes = 16384
    tool_name = "call_agent__ziwei_doushu_agent"
    middleware = ToolResultArtifactMiddleware(
        stack.artifact_service, protected_tools=frozenset({tool_name})
    )
    call = SimpleNamespace(
        tool_call={"name": tool_name, "id": "protected"}, runtime=SimpleNamespace(context=context())
    )
    try:
        message = ToolMessage(content='{"facts":[1,2,3]}', tool_call_id="protected")
        protected = middleware._project(call, message)
        assert (
            protected.content == message.content
            and protected.additional_kwargs["preserve_structure"]
        )
        oversized = message.model_copy(update={"content": "x" * 20_000})
        with pytest.raises(ValueError, match="protected"):
            middleware._project(call, oversized)
    finally:
        stack.database.close()


@pytest.mark.asyncio
async def test_handled_tool_errors_are_audited_as_failures():
    """同步、异步均检查 ToolMessage.status，不把 ToolException 的已处理结果记成功。"""
    stack = components()
    managed = stack.tool_catalog.resolve("ziwei_natal_chart", "1.0.0")
    middleware = ToolGovernanceMiddleware(
        stack.tool_catalog, stack.tool_policy, stack.audit, allowed_keys=frozenset({managed.key})
    )
    call = SimpleNamespace(
        runtime=SimpleNamespace(context=context()),
        state={"messages": []},
        tool_call={"name": managed.tool.name, "id": "error", "args": {}},
    )
    error = ToolMessage(content="safe failure", tool_call_id="error", status="error")

    async def failed(_):
        """模拟框架已把 ToolException 转换成 ToolMessage 的分支。"""
        return error

    middleware.wrap_tool_call(call, lambda _: error)
    await middleware.awrap_tool_call(call, failed)
    events = [record.event_type for record in stack.audit.records()]
    assert events.count(AuditEventType.FINANCIAL_TOOL_FAILED) == 2
    assert AuditEventType.FINANCIAL_TOOL_EXECUTED not in events
