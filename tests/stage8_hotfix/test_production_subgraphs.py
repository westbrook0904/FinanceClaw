"""Production HF-1 graphs: shared durable budget, native resume and public domain results."""

import asyncio
import json
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.types import Command

from financeclaw.agent_server.agents.offline import OfflineFinanceModel
from financeclaw.agent_server.bootstrap import build_components
from financeclaw.agent_server.tools.catalog import ToolCatalog
from financeclaw.agent_server.tools.local import MarketSnapshotTool, default_local_tools
from financeclaw.agent_server.tools.subgraph_scope import active_scope
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.releases.catalog import build_release_catalogs
from financeclaw.shared.turns.snapshots import agent_snapshot
from financeclaw.shared.turns.types import ExecutionConflict
from tests.stage6fix.test_batch_tools import BatchModel, call
from tests.stage7.support import request, settings
from tests.turn_support import cancel_execution


class SerialModel(OfflineFinanceModel):
    """The orchestrator cannot see Worker messages, only returned public Tool results."""

    calls: list[dict]

    def _generate(self, messages, *args, **kwargs):
        """根据已返回的工具结果推进确定性的测试步骤。"""
        results = [m for m in messages if isinstance(m, ToolMessage)]
        answer = (
            AIMessage(content="all workers finished")
            if len(results) == len(self.calls)
            else AIMessage(content="", tool_calls=[self.calls[len(results)]])
        )
        return ChatResult(generations=[ChatGeneration(message=answer)])


class ResearchModel(OfflineFinanceModel):
    """Exercise real leaf tools, optional native questions, and structured domain output."""

    questions: int = 0
    inputs: ClassVar[list] = []

    def _generate(self, messages, *args, **kwargs):
        """根据已返回的工具结果推进确定性的测试步骤。"""
        type(self).inputs.append([m.content for m in messages if isinstance(m, HumanMessage)])
        receipts = [m for m in messages if isinstance(m, ToolMessage)]
        if not receipts:
            item = call("market_snapshot", 1, symbol="AAPL")
        elif len(receipts) <= self.questions:
            item = call(
                "request_user__research_scope",
                len(receipts) + 1,
                question=f"研究区间 {len(receipts)}？",
            )
        else:
            item = call(
                "MarketResearchResult",
                9,
                outcome="success",
                summary="bounded research",
                evidence=[
                    {
                        "provider": "offline-fixture",
                        "as_of": "2026-09-09",
                        "summary": "synthetic market quote",
                    }
                ],
            )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[item]))]
        )


class FreshMarket(MarketSnapshotTool):
    """Synthetic prices with an injected current timestamp for freshness tests."""

    def _run(self, symbol):
        """保留行情调用计数，仅替换合成来源时间。"""
        value = json.loads(super()._run(symbol))
        value["as_of"] = datetime.now(UTC).isoformat()
        return json.dumps(value)


@pytest.fixture
def stack(tmp_path, request):
    """SQLite facts and real compiled graphs; no BFF, HTTP SDK or credentials."""
    ziwei_enabled = getattr(request, "param", False)
    if ziwei_enabled:
        pytest.importorskip("x_iztro")
        pytest.importorskip("tzdata")
    value = build_components(
        settings(
            ziwei_enabled=ziwei_enabled,
            database_url=f"sqlite+pysqlite:///{tmp_path / 'hf1.db'}",
            database_auto_create_schema=True,
            artifact_root=str(tmp_path / "artifacts"),
        ),
        enable_persistence=True,
        resource_concurrency=1,
        tool_catalog=ToolCatalog(default_local_tools(market_tool=FreshMarket())),
    )
    value.agent_factory.memory_service = None
    yield value
    value.database.close()


def root_graph(
    stack,
    calls,
    *,
    questions=0,
    limits=None,
    snapshot_change=None,
    native=False,
    hitl=False,
    root_scopes=None,
    model=None,
):
    """Register one business root, then execute its frozen candidate release."""
    profile = stack.agent_profiles.resolve("finance_agent", "1.6.0")
    context = ExecutionContext(
        tenant_id="synthetic-tenant",
        subject_id="synthetic-owner",
        turn_id="root",
        scopes=root_scopes
        if root_scopes is not None
        else {
            "market:read",
            "portfolio:review",
            "ziwei:read",
            "artifacts:read",
            "watchlist:write",
        },
        data_classification="confidential",
        request_clock="2026-09-09T01:00:00+08:00",
    )
    research = stack.agent_profiles.resolve("market_research_agent", "1.3.0")
    tool = stack.tool_catalog.resolve("call_agent__market_research_agent", "1.3.0").tool
    if hitl:
        from financeclaw.kernel.agents import ToolRef
        from financeclaw.shared.releases.subgraphs import worker_declaration

        research = research.model_copy(
            update={
                "allowed_tools": (ToolRef(tool_id="watchlist_add", version="1.0.0"),),
            }
        )
        declaration = worker_declaration(research, stack.tool_catalog, stack.model_profiles)
        tool.release, tool.declaration = research, declaration
        profile = profile.model_copy(
            update={
                "worker_manifest": tuple(
                    declaration
                    if json.loads(item)["target_id"] == "market_research_agent"
                    else item
                    for item in profile.worker_manifest
                )
            }
        )
    tool.graph = stack.agent_factory.build(
        research,
        model=HITLModel() if hitl else ResearchModel(questions=questions),
        checkpointer=None,
        fallback_models=(),
    )
    snapshot = agent_snapshot(profile, context, thread_id="native-root", input_hash="synthetic")
    if limits:
        snapshot["limits"].update(limits)
    if snapshot_change:
        snapshot_change(snapshot)
    from tests.turn_support import seed_execution

    snapshot["user_message_id"] = "root-input"
    context = seed_execution(stack.conversation_repository.execution, context, snapshot)
    graph = stack.agent_factory.build(
        profile,
        model=model or SerialModel(calls=calls),
        fallback_models=(),
        **({"checkpointer": None} if native else {}),
    )
    return graph, {"config": {"configurable": {"thread_id": "native-root"}}, "context": context}


def portfolio(index=2):
    """Keep the existing Workflow input/output contract."""
    return call(
        "call_workflow__portfolio_review",
        index,
        portfolio_name="HF1 fixture",
        positions=[{"symbol": "AAPL", "quantity": "2", "cost_basis": "80"}],
        max_snapshot_age_hours=48,
    )


def approval(payload, decision="approve"):
    """Bind the user decision to the same internal invocation and approval."""
    return {
        "type": decision,
        "arguments_hash": payload["arguments_hash"],
        "invocation_id": payload["invocation_id"],
        "approval_id": payload["approval_id"],
    }


@pytest.mark.asyncio
async def test_serial_workers_complete_inside_one_root_with_gate_one(stack):
    """Market research then Workflow approval resume returns to the same ReAct loop."""
    calls = [call("call_agent__market_research_agent", 1, task="research only AAPL"), portfolio()]
    graph, kwargs = root_graph(stack, calls)
    result = await asyncio.wait_for(
        graph.ainvoke(
            {
                "messages": [
                    HumanMessage(content="ROOT SECRET JOURNAL", id="root-input"),
                    HumanMessage(content="研究 AAPL 并检查投资组合", id="root-input"),
                ]
            },
            **kwargs,
        ),
        10,
    )
    assert "__interrupt__" in result, result
    payload = result["__interrupt__"][0].value
    assert payload["root_tool_call_id"] == "call-2" and payload["worker_version"] == "1.1.0"
    before = stack.conversation_repository.execution.get("root")
    assert before["tool_calls"] == 4  # two composite entries and two market leaf attempts
    result = await graph.ainvoke(
        Command(resume={result["__interrupt__"][0].id: approval(payload)}), **kwargs
    )
    messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in messages] == ["call-1", "call-2"]
    assert json.loads(messages[0].content)["outcome"] == "success"
    assert json.loads(messages[1].content)["status"] == "completed"
    assert json.loads(messages[1].content)["workflow_version"] == "1.1.0"
    assert result["messages"][-1].content == "all workers finished"
    assert stack.conversation_repository.execution.get("root")["turn_id"] == "root"
    assert not active_scope.get()
    assert all("ROOT SECRET JOURNAL" not in str(item) for item in ResearchModel.inputs)


@pytest.mark.asyncio
async def test_consecutive_questions_keep_outer_tool_binding_and_budget(stack):
    """Two child questions may share the parent checkpoint but resume distinct interrupts."""
    graph, kwargs = root_graph(
        stack,
        [
            call(
                "call_agent__market_research_agent",
                1,
                task="/workflow do not interpret this as a directive",
            )
        ],
        questions=2,
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="root", id="root-input")]}, **kwargs
    )
    ids = []
    for _ in range(2):
        item = result["__interrupt__"][0]
        ids.append(item.id)
        assert item.value["root_tool_call_id"] == "call-1"
        result = await graph.ainvoke(
            Command(
                resume={
                    item.id: {
                        "kind": "input",
                        "answer": {"analysis_period": "2026"},
                        "invocation_id": item.value["invocation_id"],
                    }
                }
            ),
            **kwargs,
        )
    assert len(set(ids)) == 2 and "__interrupt__" not in result
    assert len([m for m in result["messages"] if isinstance(m, ToolMessage)]) == 1
    assert stack.conversation_repository.execution.get("root")["tool_calls"] == 8


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.asyncio
async def test_repeated_workflow_invocations_have_distinct_approval_and_artifact_keys(
    stack, decision
):
    """Same arguments called twice stay isolated; rejection never publishes that second report."""
    graph, kwargs = root_graph(stack, [portfolio(1), portfolio(2)])
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="two reports", id="root-input")]}, **kwargs
    )
    first = result["__interrupt__"][0]
    result = await graph.ainvoke(Command(resume={first.id: approval(first.value)}), **kwargs)
    second = result["__interrupt__"][0]
    assert second.value["approval_id"] != first.value["approval_id"]
    assert second.value["invocation_id"] != first.value["invocation_id"]
    result = await graph.ainvoke(
        Command(resume={second.id: approval(second.value, decision)}), **kwargs
    )
    outputs = [json.loads(m.content) for m in result["messages"] if isinstance(m, ToolMessage)]
    assert outputs[1]["status"] == ("completed" if decision == "approve" else "rejected")
    if decision == "approve":
        assert outputs[0]["artifact"]["artifact_id"] != outputs[1]["artifact"]["artifact_id"]
    else:
        assert outputs[1]["artifact"] is None


@pytest.mark.parametrize("mode", ["chart_only", "interpretation"])
@pytest.mark.asyncio
@pytest.mark.parametrize("stack", [True], indirect=True)
async def test_ziwei_text_result_survives_root_tool_projection(stack, mode):
    """Real Ziwei engine and text graph preserve the Stage-7 text hotfix contract."""
    graph, kwargs = root_graph(
        stack,
        [
            call(
                "call_agent__ziwei_doushu_agent",
                1,
                task="请查看这一日的盘面",
                arguments=request(mode=mode).model_dump(mode="json"),
            )
        ],
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="紫微测试", id="root-input")]}, **kwargs
    )
    output = json.loads(next(m.content for m in result["messages"] if isinstance(m, ToolMessage)))
    assert output["outcome"] == ("chart_only" if mode == "chart_only" else "answer")
    assert output["schema_version"] == 2 and len(output["charts_used"]) == 1
    if mode == "interpretation":
        assert output["answer_text"] and "interpretations" not in output


class ClarifyingRootModel(SerialModel):
    """根模型只能从 Worker 的公开结果获知待澄清问题。"""

    def _generate(self, messages, *args, **kwargs):
        """取到澄清结果后，在根会话输出问题。"""
        if isinstance(messages[-1], ToolMessage):
            result = json.loads(messages[-1].content)
            assert result["outcome"] == "needs_clarification"
            assert result["missing_fields"] == ["target"]
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content=result["question"]))]
            )
        return super()._generate(messages, *args, **kwargs)


@pytest.mark.asyncio
async def test_market_worker_clarification_interrupts_root_before_next_model(stack):
    """基础安装同样验证通用 Worker 澄清门控，不仅对紫微工具名生效。"""
    calls = [call("call_agent__market_research_agent", 1, task="研究行情")]
    graph, kwargs = root_graph(stack, calls, limits={"model": 2})
    tool = stack.tool_catalog.resolve("call_agent__market_research_agent", "1.3.0").tool
    tool.graph = stack.agent_factory.build(
        tool.release,
        model=BatchModel(
            calls=[
                call(
                    "MarketResearchResult",
                    2,
                    outcome="needs_clarification",
                    question="请问要研究哪只股票？",
                    missing_fields=["symbols"],
                )
            ]
        ),
        checkpointer=None,
        fallback_models=(),
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="研究行情", id="root-input")]}, **kwargs
    )
    assert result["__interrupt__"][0].value["question"] == "请问要研究哪只股票？"
    assert stack.conversation_repository.execution.get("root")["model_calls"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", [True], indirect=True)
async def test_ziwei_evidence_clarification_returns_to_root_with_budget_remaining(
    stack, monkeypatch
):
    """真实根图、Worker Tool 和取证图闭环，只在根生成一个 interrupt，保留根预算。"""
    from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
    from financeclaw.agent_server.graphs.ziwei_agent import build_ziwei_agent
    from tests.stage7.test_evidence_exit import RepeatingEvidenceModel

    def missing(*args, **kwargs):
        """让预检通过后的真实取证 Tool 发现缺少查询资料。"""
        raise ZiweiError("ZIWEI_INPUT_INCOMPLETE", "请补充查询日期。", ("target",))

    monkeypatch.setattr(stack.ziwei_service, "calculate", missing)
    RepeatingEvidenceModel.calls = []
    tool = stack.tool_catalog.resolve("call_agent__ziwei_doushu_agent", "2.2.0").tool
    tool.graph = build_ziwei_agent(
        stack.agent_factory,
        tool.release,
        stack.ziwei_service,
        model=RepeatingEvidenceModel(),
    )
    invocation = call(
        tool.name,
        1,
        task="紫微测试",
        arguments=request(mode="interpretation").model_dump(mode="json"),
    )
    graph, kwargs = root_graph(
        stack,
        [invocation],
        model=ClarifyingRootModel(calls=[invocation]),
        limits={"model": 4, "tool": 3},
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="紫微测试", id="root-input")]}, **kwargs
    )
    assert result["__interrupt__"][0].value["question"] == "请补充查询日期。"
    assert len(result["__interrupt__"]) == 1
    assert RepeatingEvidenceModel.calls == ["evidence"]
    execution = stack.conversation_repository.execution.get("root")
    assert execution["model_calls"] == 2  # root dispatch + evidence; question needs no model
    assert execution["tool_calls"] == 3  # Worker entry + chart attempt + clarification


@pytest.mark.parametrize("failure", ["manifest", "budget", "cancel"])
@pytest.mark.asyncio
async def test_root_release_budget_and_cancel_fail_closed(stack, failure):
    """Wrong pinned releases, exhausted root budgets and cancellation cannot become results."""

    def corrupt(snapshot):
        """注入错配的固定清单以检验发布校验。"""
        if failure == "manifest":
            snapshot["profile"]["worker_manifest"] = []

    graph, kwargs = root_graph(
        stack,
        [call("call_agent__market_research_agent", 1, task="AAPL")],
        limits={"model": 1} if failure == "budget" else None,
        snapshot_change=corrupt,
    )
    if failure == "cancel":
        cancel_execution(stack.conversation_repository.execution, "root")
    with pytest.raises(ExecutionConflict):
        await graph.ainvoke({"messages": [HumanMessage(content="root", id="root-input")]}, **kwargs)
    assert not active_scope.get()


@pytest.mark.asyncio
async def test_revocation_while_waiting_prevents_worker_resume(stack):
    """A valid native resume does not restore a revoked user grant."""
    from financeclaw.shared.turns.tables import ConversationTurnRow

    graph, kwargs = root_graph(stack, [portfolio()])
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="review", id="root-input")]}, **kwargs
    )
    item = result["__interrupt__"][0]
    before = stack.conversation_repository.execution.get("root")["tool_calls"]
    with stack.database.session_factory.begin() as session:
        session.get(ConversationTurnRow, "root").grant_revoked = True
    with pytest.raises(ExecutionConflict, match="revoked"):
        await graph.ainvoke(Command(resume={item.id: approval(item.value)}), **kwargs)
    assert stack.conversation_repository.execution.get("root")["tool_calls"] == before


@pytest.mark.asyncio
async def test_worker_direct_invocation_and_forged_identity_are_rejected(stack):
    """A release listed in the root manifest is not independently executable."""
    graph, kwargs = root_graph(stack, [portfolio()])
    worker = stack.tool_catalog.resolve("call_agent__market_research_agent", "1.3.0").tool
    with pytest.raises(ExecutionConflict, match="trusted internal"):
        await worker.graph.ainvoke(
            {"messages": [HumanMessage(content="forged worker", id="root-input")]}, **kwargs
        )
    forged = kwargs["context"].model_copy(update={"tenant_id": "other-tenant"})
    with pytest.raises(ExecutionConflict, match="identity"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="root", id="root-input")]},
            **{**kwargs, "context": forged},
        )


@pytest.mark.asyncio
async def test_wrong_invocation_cannot_approve_a_workflow(stack):
    """A decision for a different call with identical arguments cannot publish."""
    graph, kwargs = root_graph(stack, [portfolio()])
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="review", id="root-input")]}, **kwargs
    )
    item = result["__interrupt__"][0]
    with pytest.raises(ExecutionConflict, match="does not match"):
        await graph.ainvoke(
            Command(resume={item.id: {**approval(item.value), "invocation_id": "wrong"}}), **kwargs
        )
    assert stack.conversation_repository.execution.get("root")["tool_calls"] == 3


@pytest.mark.asyncio
async def test_composite_mixed_batch_is_rejected_before_any_worker_runs(stack):
    """Composite Tools are never accidentally classified as independent READ tools."""
    calls = [portfolio(), call("market_snapshot", 3, symbol="AAPL")]
    _, kwargs = root_graph(stack, calls)
    graph = stack.agent_factory.build(
        stack.agent_profiles.resolve("finance_agent", "1.6.0"), model=BatchModel(calls=calls)
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="mixed batch", id="root-input")]}, **kwargs
    )
    assert all(
        json.loads(m.content)["error"] == "unsupported_tool_batch"
        for m in result["messages"]
        if isinstance(m, ToolMessage)
    )
    assert stack.conversation_repository.execution.get("root")["tool_calls"] == 0


@pytest.mark.asyncio
async def test_worker_leaf_retry_is_charged_to_root(stack):
    """Transient retries stay at the leaf; the composite itself is not retried."""
    stack.tool_catalog.resolve("market_snapshot", "1.0.0").tool._remaining_failures = 1
    graph, kwargs = root_graph(stack, [call("call_agent__market_research_agent", 1, task="AAPL")])
    result = await asyncio.wait_for(
        graph.ainvoke({"messages": [HumanMessage(content="research", id="root-input")]}, **kwargs),
        10,
    )
    assert result["messages"][-1].content == "all workers finished"
    assert stack.conversation_repository.execution.get("root")["tool_calls"] == 3
    assert stack.tool_catalog.resolve("market_snapshot", "1.0.0").tool.call_count == 2


@pytest.mark.asyncio
async def test_same_subagent_called_twice_keeps_completed_first_result(stack):
    """Second call resumes in its own namespace without replaying the completed first leaf."""
    calls = [call("call_agent__market_research_agent", i, task="AAPL") for i in (1, 2)]
    graph, kwargs = root_graph(stack, calls, questions=1)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="twice", id="root-input")]}, **kwargs
    )
    for i in (1, 2):
        item = result["__interrupt__"][0]
        assert item.value["root_tool_call_id"] == f"call-{i}"
        result = await graph.ainvoke(
            Command(
                resume={
                    item.id: {
                        "kind": "input",
                        "answer": {"analysis_period": "2026"},
                        "invocation_id": item.value["invocation_id"],
                    }
                }
            ),
            **kwargs,
        )
    assert stack.tool_catalog.resolve("market_snapshot", "1.0.0").tool.call_count == 2
    assert len([m for m in result["messages"] if isinstance(m, ToolMessage)]) == 2


@pytest.mark.asyncio
async def test_root_slash_directive_selects_internal_worker_tool(stack):
    """The new Tool name is honored by both preference routing and execution governance."""
    calls = [call("call_agent__market_research_agent", 1, task="AAPL")]
    graph, kwargs = root_graph(stack, calls)
    result = await graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    content='/agent market_research_agent {"task":"AAPL"}', id="root-input"
                )
            ]
        },
        **kwargs,
    )
    output = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert json.loads(output.content)["outcome"] == "success"


class HITLModel(OfflineFinanceModel):
    """Exercise a synthetic WRITE leaf through the production Worker middleware stack."""

    def _generate(self, messages, *args, **kwargs):
        """Request one governed write, then return a bounded public Worker result."""
        receipts = [m for m in messages if isinstance(m, ToolMessage)]
        item = (
            call("MarketResearchResult", 9, outcome="partial", limitations=["synthetic HITL test"])
            if receipts
            else call("watchlist_add", 1, symbol="AAPL", note="synthetic HF1")
        )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[item]))]
        )


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.asyncio
async def test_native_worker_hitl_preserves_approval_and_root_rejection(stack, decision):
    """Native HITL retains leaf governance and persists rejection before the root continues."""
    graph, kwargs = root_graph(
        stack, [call("call_agent__market_research_agent", 1, task="synthetic HITL")], hitl=True
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="root", id="root-input")]}, **kwargs
    )
    item = result["__interrupt__"][0]
    assert set(item.value) == {"action_requests", "review_configs"}
    assert item.value["action_requests"][0]["name"] == "watchlist_add"
    write = stack.tool_catalog.resolve("watchlist_add", "1.0.0").tool
    assert not write.writes
    result = await graph.ainvoke(
        Command(resume={item.id: {"decisions": [{"type": decision}]}}), **kwargs
    )
    assert len(write.writes) == (1 if decision == "approve" else 0)
    assert stack.conversation_repository.execution.get("root")["side_effects_denied"] == (
        decision == "reject"
    )
    assert result["messages"][-1].content == "all workers finished"


@pytest.mark.asyncio
async def test_root_native_hitl_rejection_blocks_subsequent_side_effects(stack):
    """HF-2 applies root HITL rejection inside the graph before another ReAct tool batch."""
    graph, kwargs = root_graph(
        stack,
        [
            call("watchlist_add", 1, symbol="AAPL", note="first"),
            call("watchlist_add", 2, symbol="MSFT", note="after rejection"),
        ],
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="synthetic root approval", id="root-input")]}, **kwargs
    )
    wait = result["__interrupt__"][0]
    result = await graph.ainvoke(
        Command(resume={wait.id: {"decisions": [{"type": "reject"}]}}), **kwargs
    )
    assert "__interrupt__" not in result
    assert stack.conversation_repository.execution.get("root")["side_effects_denied"]
    assert not stack.tool_catalog.resolve("watchlist_add", "1.0.0").tool.writes
    assert result["messages"][-1].content == "all workers finished"


def test_catalog_contains_only_current_root_and_worker_releases(stack):
    """All runtime and static contracts agree; unpublished historical releases are absent."""
    releases = build_release_catalogs(stack.settings, enable_persistence=True)
    assert set(releases.agent_profiles) == {
        ("finance_agent", "1.6.0"),
        ("market_research_agent", "1.3.0"),
        ("ziwei_doushu_agent", "2.2.0"),
    }
    assert stack.default_agent_profile.version == "1.6.0"
    assert set(stack.agent_profiles) == set(releases.agent_profiles)
    assert all(
        not ref.tool_id.startswith("delegate_") for ref in stack.default_agent_profile.allowed_tools
    )


class VisibleToolsModel(OfflineFinanceModel):
    """Expose the names actually bound to the root model after governance filtering."""

    def _generate(self, messages, *args, **kwargs):
        """Return tool names without invoking any leaf Tool or remote model."""
        return ChatResult(
            generations=[
                ChatGeneration(message=AIMessage(content=",".join(sorted(self._bound_tool_names))))
            ]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stack", [False, True], indirect=True)
@pytest.mark.parametrize("ziwei_scope", [False, True])
async def test_root_model_sees_ziwei_only_when_enabled_and_authorized(stack, ziwei_scope):
    """The feature flag registers the Worker; caller scopes control model visibility."""
    scopes = {"market:read"} | ({"ziwei:read"} if ziwei_scope else set())
    graph, kwargs = root_graph(stack, [], root_scopes=scopes, model=VisibleToolsModel())
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="List available tools.", id="root-input")]}, **kwargs
    )
    visible = set(result["messages"][-1].content.split(","))
    name = "call_agent__ziwei_doushu_agent"
    assert "call_agent__market_research_agent" in visible
    assert (name in visible) is (stack.settings.ziwei_enabled and ziwei_scope)
    declared = {json.loads(item)["tool_id"] for item in stack.default_agent_profile.worker_manifest}
    assert (name in declared) is stack.settings.ziwei_enabled
