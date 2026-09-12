"""Transactional admission, complete closures and question-aware interaction evidence."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from financeclaw.shared.conversation.tables import ConversationMessageRow
from financeclaw.shared.memory.evidence import EvidenceReader
from financeclaw.shared.memory.intake import MemoryIntake
from financeclaw.shared.memory.lifecycle import MemoryLifecycle
from financeclaw.shared.memory.models import MemoryPermissionError
from financeclaw.shared.memory.policies import explicit_preferences
from financeclaw.shared.memory.repository import canonical_hash
from financeclaw.shared.memory.tables import MemoryRecordRow, MemorySourceRow
from financeclaw.shared.outbox.tables import OutboxEventRow
from financeclaw.shared.turns.tables import ConversationTurnRow, InteractionRow
from tests.stage11.domain_support import add_user_source, create_domain


@pytest.fixture
def domain(tmp_path):
    """Keep all source identities, business objects and receipts in one isolated SQL schema."""
    stack = create_domain(tmp_path)
    yield stack
    stack[0].close()


def interaction(
    actor, context, *, question="请补充长期交流偏好", answer="以后用中文回答，简短一些"
):
    """Build an already identity-verified response with its frozen input schema."""
    request = {
        "point": {
            "point_id": "clarification",
            "kind": "input",
            "question": question,
            "response_schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        },
        "action_hash": None,
    }
    response = {"revision": 1, "kind": "input", "answer": {"answer": answer}}
    return InteractionRow(
        interaction_id="interaction-one",
        turn_id=context.turn_id,
        origin_command_id=context.command_id,
        native_run_id="native-one",
        checkpoint={},
        interrupt_id="interrupt-one",
        revision=1,
        kind="input",
        request=request,
        request_hash=canonical_hash(request),
        status="resolved",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        decided_at=datetime.now(UTC),
        decided_by=actor.subject_id,
        response=response,
        response_hash=canonical_hash(response),
    )


def test_persistent_preference_supports_omitted_repeated_verb():
    """Common clause ellipsis remains bounded by a full persistent instruction match."""
    assert explicit_preferences("以后都用中文回答，简短一些") == {
        "language": "zh-CN",
        "verbosity": "concise",
    }
    assert explicit_preferences("这次用中文回答，简短一些") == {}
    assert explicit_preferences("假设以后都用中文回答，简短一些") == {}


def test_accepted_form_assertion_can_commit_low_risk_without_finishing_turn(domain):
    """S07: accepted answers include their question and can safely commit explicit preferences."""
    database, _, _, _, repository = domain
    actor, _, context = add_user_source(domain, "准备研究计划")
    intake = MemoryIntake(database.session_factory)
    with database.session_factory.begin() as session:
        session.add(interaction(actor, context))
        source = intake.register_interaction_in_session(session, actor, "interaction-one")
    assert {row.field: row.content for row in repository.list_records(actor)} == {
        "language": "zh-CN",
        "verbosity": "concise",
    }
    first = repository.owner_snapshot(actor)
    with database.session_factory.begin() as session:
        assert (
            intake.register_interaction_in_session(session, actor, "interaction-one").source_id
            == source.source_id
        )
        documents = EvidenceReader().read_in_session(session, actor, (source.evidence_ref(),))
        assert "请补充长期交流偏好" in documents[0].content
        assert "简短一些" in documents[0].content
    assert repository.owner_snapshot(actor) == first


@pytest.mark.parametrize(
    "question,answer",
    [
        ("这次选择什么语言", "以后用中文回答"),
        ("假设你以后偏好中文", "以后用中文回答"),
        ("以后用中文回答吗", "是"),
        ("请输入语言代码", "zh-CN"),
    ],
)
def test_question_or_bare_answer_cannot_invent_a_persistent_preference(domain, question, answer):
    """Question templates and unqualified option codes are never enough to prove scope."""
    database = domain[0]
    actor, _, context = add_user_source(domain, "准备研究计划")
    with database.session_factory.begin() as session:
        session.add(interaction(actor, context, question=question, answer=answer))
        MemoryIntake(database.session_factory).register_interaction_in_session(
            session, actor, "interaction-one"
        )
        assert session.scalar(select(func.count()).select_from(MemoryRecordRow)) == 0


def test_complete_turn_closure_contains_user_and_final_context_source(domain):
    """S15: original input, final assistant source and durable prepare intent commit together."""
    database, conversations, _, _, _ = domain
    actor, source, _ = add_user_source(domain, "比较两种住房储蓄计划")
    intake = MemoryIntake(database.session_factory)
    with database.session_factory.begin() as session:
        conversations.append_assistant_message(
            turn_id=actor.turn_id, content="已比较两种计划及其假设", session=session
        )
        session.get(ConversationTurnRow, actor.turn_id).status = "completed"
        event_id = intake.close_turn_in_session(
            session, actor, actor.turn_id, model_profile_version="frozen-model"
        )
    with database.session_factory.begin() as session:
        event = session.get(OutboxEventRow, event_id)
        assert {ref["source_kind"] for ref in event.payload["sources"]} == {
            "user_message",
            "assistant_message",
        }
        assert event.payload["sources"][0]["source_id"] == source.source_id
        assert (
            intake.close_turn_in_session(
                session, actor, actor.turn_id, model_profile_version="frozen-model"
            )
            == event_id
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(OutboxEventRow.destination == "memory_extract")
            )
            == 1
        )


def test_failed_turn_does_not_close_completed_task_memory(domain):
    """S08: a failed task can retain prior explicit preferences without claiming completion."""
    database = domain[0]
    actor, _, _ = add_user_source(domain, "以后用中文回答", auto_commit=True)
    with database.session_factory.begin() as session:
        session.get(ConversationTurnRow, actor.turn_id).status = "failed"
        assert (
            MemoryIntake(database.session_factory).close_turn_in_session(
                session, actor, actor.turn_id
            )
            is None
        )
    assert domain[4].list_records(actor)[0].content == "zh-CN"


def test_hidden_journal_source_changes_authority_and_audit_in_one_transaction(domain):
    """S35: the public domain lifecycle hides the original Journal and its derived profile."""
    database = domain[0]
    actor, source, _ = add_user_source(domain, "以后用中文回答", auto_commit=True)
    lifecycle = MemoryLifecycle(database.session_factory)
    result = lifecycle.hide_source(
        actor, source.source_id, mutation_id="hide", expected_source_version=1
    )
    assert result["privacy_epoch"] == 1
    assert (
        lifecycle.hide_source(
            actor, source.source_id, mutation_id="hide", expected_source_version=1
        )
        == result
    )
    with database.session_factory() as session:
        assert not session.get(ConversationMessageRow, source.object_id).visible
        assert not session.get(MemorySourceRow, source.source_id).visible
    assert domain[4].list_records(actor) == ()
    with pytest.raises(MemoryPermissionError):
        lifecycle.hide_source(
            actor.model_copy(update={"kind": "worker"}), source.source_id, mutation_id="worker-hide"
        )


def test_configured_permit_expiry_is_frozen_at_admission(domain):
    """The configured derive lifetime is a persistent deadline, not a retry-relative timer."""
    database = domain[0]
    actor, source, _ = add_user_source(domain, "已有来源")
    # An admitted source keeps its first deadline even if another process has new settings.
    first_expiry = source.permit.expires_at
    with database.session_factory.begin() as session:
        repeated = MemoryIntake(
            database.session_factory, permit_seconds=60
        ).register_message_in_session(session, actor, source.object_id)
    assert repeated.permit.expires_at == first_expiry
