"""Frozen SQL recall, untrusted index results and finite optional-memory budgets."""

from types import SimpleNamespace

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from financeclaw.agent_server.context.planning import projected_messages
from financeclaw.agent_server.context.state import ConversationState
from financeclaw.agent_server.memory.recall import bounded_query
from financeclaw.agent_server.middleware.final_context import (
    FinalContextMiddleware,
    RequestRecorder,
)
from financeclaw.agent_server.middleware.memory_middleware import MemoryRecallMiddleware
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.models import ModelProfile
from financeclaw.shared.llm.budget import ContextBudget, ContextBudgetPlanner
from financeclaw.shared.memory.intake import MemoryIntake
from financeclaw.shared.memory.models import MemoryActor, MemoryMutation
from tests.stage3.support import conversation_context
from tests.stage9.conftest import memory_stack as memory_stack
from tests.stage11.test_context_native import BoundFakeModel
from tests.stage11.test_domain_intake import interaction


class SearchIndex:
    """Count optional semantic reads and return deliberately untrusted projection bodies."""

    def __init__(self, values=()):
        """Keep index discovery independent of authoritative SQL content."""
        self.values, self.calls = values, 0
        self.queries = []

    def search(self, *args, **kwargs):
        """Return the configured stale or valid identifiers without altering SQL."""
        self.calls += 1
        self.queries.append(kwargs.get("query"))
        return [SimpleNamespace(value=value) for value in self.values]


def test_background_updates_wait_for_next_turn_but_forget_invalidates_immediately(memory_stack):
    """Keep old readable revisions stable within a Turn and prioritize privacy over stability."""
    context, message_id, repository, _, service, _ = memory_stack
    actor = MemoryActor(
        tenant_id=context.tenant_id, subject_id=context.subject_id, scopes=context.scopes
    )
    first = service.mutations.apply(
        actor,
        MemoryMutation(
            mutation_id="language-first",
            kind="profile",
            field="language",
            content="zh-CN",
            explicit_intent=True,
        ),
    )
    middleware, index = MemoryRecallMiddleware(service), SearchIndex()
    state = {"messages": [HumanMessage(content="分析今天的新任务", id=message_id)]}
    runtime = SimpleNamespace(context=context, store=index)
    state.update(middleware.before_model(state, runtime))
    assert index.calls == 0
    assert '"content": "zh-CN"' in state["memory_recall"]["projection"]
    updated = service.mutations.apply(
        actor,
        MemoryMutation(
            mutation_id="language-second",
            operation="update",
            memory_id=first.memory_id,
            expected_revision=first.revision,
            kind="profile",
            field="language",
            content="en",
            explicit_intent=True,
        ),
    )
    assert middleware.before_model(state, runtime) is None
    assert '"content": "zh-CN"' in middleware._render(context, state["memory_recall"])[0]
    next_context, next_id = conversation_context(repository, key="next-memory-turn")
    next_state = {"messages": [HumanMessage(content="另一个任务", id=next_id)]}
    next_result = middleware.before_model(
        next_state, SimpleNamespace(context=next_context, store=index)
    )
    assert '"content": "en"' in next_result["memory_recall"]["projection"]
    service.mutations.apply(
        actor,
        MemoryMutation(
            mutation_id="language-forgotten",
            operation="forget",
            memory_id=updated.memory_id,
            expected_revision=updated.revision,
            explicit_intent=True,
        ),
    )
    assert middleware._render(context, state["memory_recall"])[0] == ""
    assert middleware.before_model(state, runtime)["memory_recall"]["profile"] == []


def test_untrusted_index_text_is_ignored_and_empty_index_has_sql_fallback(memory_stack):
    """Validate owner and exact revision before retrieval, including asynchronous index lag."""
    context, _, _, _, service, _ = memory_stack
    actor = MemoryActor(
        tenant_id=context.tenant_id, subject_id=context.subject_id, scopes=context.scopes
    )
    task = service.mutations.apply(
        actor,
        MemoryMutation(
            mutation_id="bond-analysis",
            kind="task",
            content="完成了债券久期研究",
            explicit_intent=True,
        ),
    )
    index = SearchIndex(
        [
            {"memory_id": "foreign", "revision": 1, "content": "fabricated"},
            {"memory_id": task.memory_id, "revision": task.revision, "content": "injected"},
        ]
    )
    result = service.search(context, index, query="之前的债券研究")
    assert len(result) == 1 and result[0].content == "完成了债券久期研究"
    for index in (
        None,
        SearchIndex(),
        SearchIndex([{"memory_id": task.memory_id, "revision": 999, "content": "stale"}]),
    ):
        assert (
            service.search(context, index, query="继续上次的债券研究")[0].memory_id
            == task.memory_id
        )


def test_store_failure_exposes_only_bounded_degradation_metadata(memory_stack, caplog):
    """S40: SQL still answers while the log excludes the query and the provider's raw error."""
    context, _, _, _, service, _ = memory_stack

    class UnavailableIndex:
        """Simulate a native index timeout containing data that cannot be logged."""

        def search(self, *args, **kwargs):
            """Fail without granting permission to publish the diagnostic payload."""
            raise TimeoutError("private-provider-payload")

    with caplog.at_level("INFO", logger="financeclaw.agent_server.memory.recall"):
        assert service.search(context, UnavailableIndex(), query="private-user-query") == ()
    assert "reason=store_error" in caplog.text
    assert "elapsed_seconds=" in caplog.text
    assert "private-provider-payload" not in caplog.text
    assert "private-user-query" not in caplog.text


def test_optional_profile_omissions_are_explained_without_blocking_user(memory_stack):
    """Record a bounded omission instead of failing a valid current user request."""
    context, message_id, _, _, service, _ = memory_stack
    service.mutations.apply(
        MemoryActor(
            tenant_id=context.tenant_id, subject_id=context.subject_id, scopes=context.scopes
        ),
        MemoryMutation(
            mutation_id="profile-over-budget",
            kind="profile",
            field="language",
            content="zh-CN",
            explicit_intent=True,
        ),
    )
    result = MemoryRecallMiddleware(service, profile_tokens=1).before_model(
        {"messages": [HumanMessage(content="当前任务不能丢失", id=message_id)]},
        SimpleNamespace(context=context, store=None),
    )
    assert result["memory_recall"]["projection"] == ""
    omission = result["memory_recall"]["omissions"][0]
    assert omission["reason"] == "token_budget" and omission["item_type"] == "memory"
    assert omission["token_count"] > 1
    query = bounded_query("最初任务" + "中间内容" * 200 + "最后纠正：关注债券")
    assert (
        len(query) <= 512 and query.startswith("最初任务") and query.endswith("最后纠正：关注债券")
    )


def test_full_request_budget_omits_optional_memory_before_rejecting_user(memory_stack):
    """S46: schemas and mandatory input fit the smaller fallback after a whole memory yields."""
    context, message_id, _, _, service, _ = memory_stack
    service.mutations.apply(
        MemoryActor(
            tenant_id=context.tenant_id, subject_id=context.subject_id, scopes=context.scopes
        ),
        MemoryMutation(
            mutation_id="optional-language",
            kind="profile",
            field="language",
            content="zh-CN",
            explicit_intent=True,
        ),
    )
    profile = ModelProfile(
        profile_id="capacity",
        version="1.0.0",
        model="offline",
        context_window_tokens=32000,
        max_tokens=1000,
        token_estimator="utf8-bytes-v1",
    )
    tools = (
        {
            "name": "compute",
            "description": "schema" * 80,
            "parameters": {
                "type": "object",
                "properties": {"amount": {"type": "number"}},
            },
        },
    )
    output_schema = {"type": "object", "description": "output" * 80}
    state = {
        "messages": [HumanMessage(content="Original required user input " * 60, id=message_id)]
    }
    runtime = SimpleNamespace(context=context, store=None)
    unbounded = MemoryRecallMiddleware(service)
    state.update(unbounded.before_model(state, runtime))
    system = "Important fixed instructions " * 40
    base = ContextBudgetPlanner(profile, 32000)
    full = base.estimate(
        projected_messages(state, system_prompt=system), tools=tools, output_schema=output_schema
    )
    limit = full - 200
    smaller = profile.model_copy(
        update={"profile_id": "smaller", "context_window_tokens": limit + 1000}
    )
    planner = ContextBudgetPlanner(profile, 32000, fallback_profiles=(smaller,))
    assert planner.input_limit == limit
    middleware = MemoryRecallMiddleware(
        service, planner=planner, system_prompt=system, tools=tools, output_schema=output_schema
    )
    original = state["messages"][0].model_dump(mode="json")
    state.update(middleware.before_model(state, runtime))
    assert state["memory_recall"]["projection"] == ""
    assert state["memory_recall"]["omissions"][0]["item_type"] == "memory"
    assert state["messages"][0].model_dump(mode="json") == original
    planner.check(
        projected_messages(state, system_prompt=system), tools=tools, output_schema=output_schema
    )
    request = ModelRequest(
        model=object(),
        messages=state["messages"],
        system_message=SystemMessage(content=system),
        tools=list(tools),
        state=state,
        runtime=runtime,
    )
    actual = middleware._apply(request)
    planner.check(
        [actual.system_message, *actual.messages], tools=tools, output_schema=output_schema
    )
    assert actual.system_message.additional_kwargs["financeclaw_memory_refs"] == []
    assert (
        actual.system_message.additional_kwargs["financeclaw_memory_omissions"]
        == state["memory_recall"]["omissions"]
    )


def test_accepted_answer_refreshes_cached_empty_search_and_only_its_own_profile(memory_stack):
    """S07/S10: new trusted answers change L1; an unrelated background profile stays frozen."""
    context, message_id, _, _, service, _ = memory_stack
    actor = service.actor(context).model_copy(update={"kind": "user"})
    first = service.mutations.apply(
        actor,
        MemoryMutation(
            mutation_id="original-style",
            kind="profile",
            field="verbosity",
            content="concise",
            explicit_intent=True,
        ),
    )
    middleware, index = MemoryRecallMiddleware(service), SearchIndex()
    state = {"messages": [HumanMessage(content="继续之前的研究", id=message_id)]}
    runtime = SimpleNamespace(context=context, store=index)
    state.update(middleware.before_model(state, runtime))
    assert index.calls == 1 and state["memory_recall"]["tasks"] == []
    assert middleware.before_model(state, runtime) is None and index.calls == 1
    service.mutations.apply(
        actor,
        MemoryMutation(
            mutation_id="background-style",
            operation="update",
            memory_id=first.memory_id,
            expected_revision=first.revision,
            kind="profile",
            field="verbosity",
            content="detailed",
            explicit_intent=True,
        ),
    )
    with service.repository.sessions.begin() as session:
        session.add(
            interaction(
                actor,
                context,
                question="继续之前的债券研究，应使用什么语言回答？",
                answer="以后用英文回答",
            )
        )
        MemoryIntake(service.repository.sessions).register_interaction_in_session(
            session, actor, "interaction-one"
        )
    state.update(middleware.before_model(state, runtime))
    assert index.calls == 2 and "债券研究" in index.queries[-1]
    assert '"content": "en"' in state["memory_recall"]["projection"]
    assert '"content": "concise"' in state["memory_recall"]["projection"]
    assert '"content": "detailed"' not in state["memory_recall"]["projection"]
    assert middleware.before_model(state, runtime) is None and index.calls == 2


def test_native_checkpoint_persists_memory_omission_and_records_actual_manifest(memory_stack):
    """Native before-model/reducer/final guard preserve raw input while optional SQL data yields."""
    context, message_id, repository, _, service, _ = memory_stack
    service.mutations.apply(
        MemoryActor(
            tenant_id=context.tenant_id, subject_id=context.subject_id, scopes=context.scopes
        ),
        MemoryMutation(
            mutation_id="native-optional",
            kind="profile",
            field="language",
            content="zh-CN",
            explicit_intent=True,
        ),
    )

    @tool
    def calculate(amount: int) -> int:
        """Return an amount without external effects; this test must not execute the tool."""
        raise AssertionError("the model already has the requested result")

    profile = ModelProfile(
        profile_id="native-memory",
        version="1.0.0",
        model="offline",
        context_window_tokens=32000,
        max_tokens=1000,
        token_estimator="utf8-bytes-v1",
    )
    system = "Complete the user's current task. " * 30
    message = HumanMessage(content="Necessary original user input " * 60, id=message_id)
    initial = {"messages": [message]}
    initial.update(
        MemoryRecallMiddleware(service).before_model(
            initial, SimpleNamespace(context=context, store=None)
        )
    )
    tokens = ContextBudgetPlanner(profile, 32000).estimate(
        projected_messages(initial, system_prompt=system), tools=(calculate,)
    )
    planner = ContextBudgetPlanner(profile, tokens - 200)
    budget = ContextBudget(
        model_input_limit=tokens - 200,
        reserved_output_tokens=1000,
        system_policy_reserve=0,
        tool_schema_reserve=0,
        safety_margin=0,
    )
    model = BoundFakeModel(
        responses=[AIMessage(content="Task completed")],
        metadata={"financeclaw_model_profile": profile.model_dump(mode="json")},
    )
    graph = create_agent(
        model=model,
        tools=[calculate],
        system_prompt=system,
        middleware=[
            MemoryRecallMiddleware(
                service, planner=planner, system_prompt=system, tools=(calculate,)
            ),
            FinalContextMiddleware(RequestRecorder(budget, repository, planner=planner)),
        ],
        context_schema=ExecutionContext,
        state_schema=ConversationState,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "native-memory-capacity"}}
    result = graph.invoke({"messages": [message]}, config=config, context=context)
    saved = graph.get_state(config).values
    assert result["messages"][-1].content == "Task completed"
    assert saved["messages"][0].content == message.content
    assert saved["memory_recall"]["projection"] == ""
    assert saved["memory_recall"]["budget_exclusions"]
    manifest = repository.list_manifests(context.conversation_id)[0]
    assert manifest.input_token_count <= manifest.available_input_tokens
    assert manifest.memory_refs == ()
    assert manifest.omissions[0].item_type == "memory"
