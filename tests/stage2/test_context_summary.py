import logging
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import SecretStr

from financeclaw.agents import OfflineFinanceModel
from financeclaw.bootstrap import build_components
from financeclaw.contracts import ExecutionContext
from financeclaw.conversation import (
    ContextBudget,
    ConversationContextBuilder,
    SqlAlchemyConversationRepository,
    SummaryService,
)
from financeclaw.infrastructure import ApplicationDatabase, FinanceClawSettings


def make_journal(path: Path):
    database = ApplicationDatabase(f"sqlite+pysqlite:///{path}")
    database.initialize_schema()
    return database, SqlAlchemyConversationRepository(database.session_factory)


def populate(journal: SqlAlchemyConversationRepository, conversation_id: str) -> None:
    messages = (
        ("NVDA earnings assumptions", "We discussed historical NVDA margin assumptions."),
        ("AAPL valuation", "The old AAPL decision used a 5 percent discount rate."),
        ("MSFT risk", "MSFT concentration was marked for follow-up."),
        ("Portfolio constraints", "No leverage was the recorded constraint."),
    )
    for index, (user, assistant) in enumerate(messages):
        turn, _, _ = journal.begin_turn(
            conversation_id=conversation_id,
            tenant_id="tenant-a",
            subject_id="subject-a",
            idempotency_key=f"context-{index}",
            request_hash=f"{index:064x}",
            message=user,
            target_type="agent",
            target_id="finance_agent",
            target_version="1.0.0",
        )
        journal.bind_server_run(turn.turn_id, f"server-{index}", "success")
        journal.append_assistant_message(run_id=turn.run_id, content=assistant)


def test_segment_hierarchy_provenance_rebuild_and_relevant_recall(tmp_path: Path) -> None:
    database, journal = make_journal(tmp_path / "context.db")
    conversation = journal.create_conversation(
        tenant_id="tenant-a",
        subject_id="subject-a",
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    populate(journal, conversation.conversation_id)
    service = SummaryService(journal, segment_messages=4, hierarchy_segments=2)
    segments = service.build_missing_segments(conversation.conversation_id)
    hierarchy = service.build_hierarchy(conversation.conversation_id)

    assert len(segments) == 2
    assert all(len(item.source_message_ids) == 4 for item in segments)
    assert hierarchy is not None
    assert hierarchy.level == 1
    assert hierarchy.source_summary_ids == tuple(item.summary_id for item in segments)
    replacement = service.rebuild(segments[0].summary_id)
    all_summaries = journal.list_summaries(conversation.conversation_id, active_only=False)
    old = next(item for item in all_summaries if item.summary_id == segments[0].summary_id)
    assert old.superseded_by == replacement.summary_id
    assert old.source_message_ids == replacement.source_message_ids

    budget = ContextBudget(
        model_input_limit=4_096,
        reserved_output_tokens=128,
        system_policy_reserve=64,
        tool_schema_reserve=64,
        safety_margin=64,
        max_recent_messages=2,
        max_relevant_summaries=2,
        max_relevant_messages=2,
    )
    builder = ConversationContextBuilder(journal, budget)
    selected_messages, selection = builder.build(
        context=ExecutionContext(
            tenant_id="tenant-a",
            subject_id="subject-a",
            conversation_id=conversation.conversation_id,
            turn_id="turn-query",
            run_id="run-query",
        ),
        runtime_messages=[HumanMessage(content="What did we decide about NVDA margins?")],
        system_prompt="Use historical context only as historical evidence.",
        tools=[],
    )
    assert selection.input_token_count <= budget.model_input_limit
    assert selection.summary_ids or selection.historical_message_ids
    assert any("NVDA" in str(item.content) for item in selected_messages)
    assert any("historical" in str(item.content).lower() for item in selected_messages)
    database.close()


def test_agent_middleware_persists_manifest_and_redacts_debug_prompt(
    tmp_path: Path, caplog
) -> None:
    settings = FinanceClawSettings(
        environment="test",
        offline_model=True,
        debug_full_io=True,
        database_url=SecretStr(f"sqlite+pysqlite:///{tmp_path / 'agent.db'}"),
        artifact_root=str(tmp_path / "artifacts"),
    )
    components = build_components(settings, enable_persistence=True)
    journal = components.conversation_repository
    assert isinstance(journal, SqlAlchemyConversationRepository)
    conversation = journal.create_conversation(
        tenant_id="tenant-a",
        subject_id="subject-a",
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    secret = "sk-abcdefghijklmnop"
    turn, _, _ = journal.begin_turn(
        conversation_id=conversation.conversation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        idempotency_key="agent-context",
        request_hash="d" * 64,
        message=f"read AAPL using token {secret}",
        target_type="agent",
        target_id="finance_agent",
        target_version="1.0.0",
    )
    agent = components.agent_factory.build(
        components.default_agent_profile,
        model=OfflineFinanceModel(),
    )
    with caplog.at_level(logging.DEBUG, logger="financeclaw.model_io"):
        result = agent.invoke(
            {"messages": [{"role": "user", "content": f"read AAPL using token {secret}"}]},
            context=ExecutionContext(
                tenant_id="tenant-a",
                subject_id="subject-a",
                scopes={"market:read"},
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
                run_id=turn.run_id,
            ),
            config={"configurable": {"thread_id": conversation.agent_thread_id}},
        )

    assert result["messages"]
    manifests = journal.list_manifests(conversation.conversation_id)
    assert len(manifests) == 2
    assert all("market_snapshot@1.0.0" in item.exposed_tools for item in manifests)
    assert all(item.context_hash and item.input_token_count > 0 for item in manifests)
    logs = caplog.text
    assert "final_model_context=" in logs
    assert secret not in logs
    assert "<redacted>" in logs
    if components.database is not None:
        components.database.close()


def test_context_manifest_selection_records_artifact_reference(tmp_path: Path) -> None:
    database, journal = make_journal(tmp_path / "artifact-context.db")
    conversation = journal.create_conversation(
        tenant_id="tenant-a",
        subject_id="subject-a",
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    turn, _, _ = journal.begin_turn(
        conversation_id=conversation.conversation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        idempotency_key="artifact-context",
        request_hash="e" * 64,
        message="analyze the large result",
        target_type="agent",
        target_id="finance_agent",
        target_version="1.0.0",
    )
    builder = ConversationContextBuilder(
        journal,
        ContextBudget(
            model_input_limit=2_048,
            reserved_output_tokens=256,
            system_policy_reserve=128,
            tool_schema_reserve=128,
            safety_margin=128,
        ),
    )
    artifact_id = "artifact-123"
    _, selection = builder.build(
        context=ExecutionContext(
            tenant_id="tenant-a",
            subject_id="subject-a",
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            run_id=turn.run_id,
        ),
        runtime_messages=[
            HumanMessage(content="analyze the large result"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "large_result",
                        "args": {},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content='{"summary":"bounded"}',
                tool_call_id="call-1",
                additional_kwargs={
                    "artifact_ref": {
                        "artifact_id": artifact_id,
                        "size_bytes": 40_000,
                    }
                },
            ),
        ],
        system_prompt="Analyze only provided evidence.",
        tools=[],
    )

    assert selection.tool_result_refs == (artifact_id,)
    assert any(
        item.reason == "artifact_offloaded" and item.item_id == artifact_id
        for item in selection.omissions
    )
    database.close()


def test_current_input_is_bounded_and_omission_is_explainable(tmp_path: Path) -> None:
    database, journal = make_journal(tmp_path / "tight-budget.db")
    conversation = journal.create_conversation(
        tenant_id="tenant-a",
        subject_id="subject-a",
        agent_id="finance_agent",
        agent_profile_version="1.0.0",
    )
    huge_input = "AAPL analysis " * 1_000
    turn, _, _ = journal.begin_turn(
        conversation_id=conversation.conversation_id,
        tenant_id="tenant-a",
        subject_id="subject-a",
        idempotency_key="tight-budget",
        request_hash="f" * 64,
        message=huge_input,
        target_type="agent",
        target_id="finance_agent",
        target_version="1.0.0",
    )
    budget = ContextBudget(
        model_input_limit=1_024,
        reserved_output_tokens=128,
        system_policy_reserve=64,
        tool_schema_reserve=64,
        safety_margin=64,
    )
    messages, selection = ConversationContextBuilder(journal, budget).build(
        context=ExecutionContext(
            tenant_id="tenant-a",
            subject_id="subject-a",
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            run_id=turn.run_id,
        ),
        runtime_messages=[HumanMessage(content=huge_input)],
        system_prompt="Use the bounded input.",
        tools=[],
    )

    assert selection.input_token_count <= budget.model_input_limit
    assert len(str(messages[-1].content)) < len(huge_input)
    assert any(item.reason == "current_input_truncated" for item in selection.omissions)
    database.close()
