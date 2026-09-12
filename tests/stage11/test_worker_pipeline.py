"""Real domain/SQL pipeline with explicit offline structured model fixtures."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from financeclaw.kernel.models import ModelProfile
from financeclaw.memory_worker.consolidation import ConsolidationHandler
from financeclaw.memory_worker.extraction import ExtractionHandler
from financeclaw.memory_worker.model import OfflineMemoryModel, StructuredMemoryModel
from financeclaw.shared.conversation.tables import ConversationMessageRow
from financeclaw.shared.memory.intake import MemoryIntake
from financeclaw.shared.memory.models import MemoryActor, MemoryConflict, MemoryMutation
from financeclaw.shared.memory.mutations import MemoryMutationService
from financeclaw.shared.memory.repository import canonical_hash, content_hash
from financeclaw.shared.memory.scheduling import ensure_consolidation_in_session
from financeclaw.shared.memory.tables import MemoryExtractionRow, MemoryOwnerRow, MemoryRecordRow
from financeclaw.shared.outbox.repository import SqlAlchemyOutboxRepository
from financeclaw.shared.outbox.tables import OutboxEventRow
from financeclaw.shared.turns.tables import ConversationTurnRow
from tests.stage3.support import conversation_context, journal


@pytest.fixture
def pipeline(tmp_path):
    """Register a real user Journal source and freeze an offline model profile."""
    database, conversations = journal(tmp_path / "pipeline.db")
    context, message_id = conversation_context(
        conversations, message="研究长期债券的风险和期限影响"
    )
    actor = MemoryActor(
        tenant_id=context.tenant_id,
        subject_id=context.subject_id,
        turn_id=context.turn_id,
        conversation_id=context.conversation_id,
        scopes=context.scopes,
    )
    with database.session_factory.begin() as session:
        source = MemoryIntake(database.session_factory).register_message_in_session(
            session, actor, message_id
        )
    outbox = SqlAlchemyOutboxRepository(database.session_factory)
    profile = ModelProfile(
        profile_id="memory-test",
        version="1.0.0",
        model="offline",
        max_tokens=2000,
        context_window_tokens=64000,
        token_estimator="utf8-bytes-v1",
    )
    offline = OfflineMemoryModel()
    model = StructuredMemoryModel(offline, profile, outbox)
    handler = ExtractionHandler(
        database.session_factory, outbox, model, consolidation_model_version=model.fingerprint
    )
    with database.session_factory.begin() as session:
        session.get(ConversationTurnRow, actor.turn_id).status = "completed"
        session.add(
            ConversationMessageRow(
                message_id="final",
                turn_id=actor.turn_id,
                conversation_id=actor.conversation_id,
                sequence=2,
                role="assistant",
                content="已完成债券久期研究，说明了风险。",
                content_hash=content_hash("已完成债券久期研究，说明了风险。"),
            )
        )
        session.flush()
        MemoryIntake(database.session_factory).close_turn_in_session(
            session,
            actor,
            actor.turn_id,
            model_profile_version=model.fingerprint,
        )
    yield database, outbox, model, offline, handler, actor, source
    database.close()


async def extract(pipeline, output):
    """Execute prepare followed by its sole child through actual transaction methods."""
    _, outbox, _, offline, handler, _, _ = pipeline
    await handler.process(outbox.claim_pending(destination="memory_extract", limit=1)[0])
    offline.outputs.append(output)
    part = outbox.claim_pending(destination="memory_extract", limit=1)[0]
    await handler.process(part)
    return part


def suggestions(source, actor):
    """Provide one explicit source-scoped task fact, never a user-level instruction."""
    return {
        "suggestions": [
            {
                "kind": "task",
                "content": "分析过长期债券风险与期限影响。",
                "scope_type": "conversation",
                "scope_id": actor.conversation_id,
                "evidence": [{"source_id": source.source_id}],
            }
        ]
    }


@pytest.mark.asyncio
async def test_empty_extraction_completes_without_record_or_second_model(pipeline):
    """S01/S18: empty output closes once and is consumed without a consolidation call."""
    database, outbox, model, offline, _, actor, _ = pipeline
    await extract(pipeline, {"suggestions": []})
    with database.session_factory.begin() as session:
        owner = session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id))
        session.get(
            OutboxEventRow, owner.active_consolidation_event_id
        ).available_at = datetime.now(UTC)
    event = outbox.claim_pending(destination="memory_consolidate", limit=1)[0]
    await ConsolidationHandler(database.session_factory, outbox, model).process(event)
    with database.session_factory() as session:
        owner = session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id))
        assert owner.extraction_revision == owner.consolidated_revision == 1
        assert owner.active_consolidation_event_id is None
        assert list(session.scalars(select(MemoryRecordRow))) == []
    assert len(offline.calls) == 1


@pytest.mark.asyncio
async def test_extraction_consolidation_and_index_intent_commit(pipeline):
    """S04: complete parts become a SQL fact and durable index intent, independently of Turn."""
    database, outbox, model, offline, _, actor, source = pipeline
    output = suggestions(source, actor)
    await extract(pipeline, output)
    with database.session_factory.begin() as session:
        owner = session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id))
        session.get(
            OutboxEventRow, owner.active_consolidation_event_id
        ).available_at = datetime.now(UTC)
    event = outbox.claim_pending(destination="memory_consolidate", limit=1)[0]
    offline.outputs.append(output)
    await ConsolidationHandler(database.session_factory, outbox, model).process(event)
    with database.session_factory() as session:
        records = list(session.scalars(select(MemoryRecordRow)))
        assert len(records) == 1 and records[0].status == "active"
        assert session.scalar(select(MemoryExtractionRow)).disposition == "consumed"
    assert len(outbox.claim_pending(destination="memory_index", limit=5)) == 1


@pytest.mark.asyncio
async def test_expired_source_is_successful_skip_without_model(pipeline):
    """S37: an explicitly revoked duty does not enter normal retries or call a model."""
    database, outbox, _, offline, handler, _, source = pipeline
    from financeclaw.shared.memory.tables import MemorySourceRow

    with database.session_factory.begin() as session:
        session.get(MemorySourceRow, source.source_id).permit_revoked = True
    event = outbox.claim_pending(destination="memory_extract", limit=1)[0]
    await handler.process(event)
    assert not offline.calls
    assert outbox.get(event.event_id).processing_metadata["outcome"] == "source_ineligible"


@pytest.mark.asyncio
async def test_model_cannot_invent_foreign_source(pipeline):
    """S51: a generated citation cannot extend the claimed source authority."""
    database, outbox, _, _, _, actor, source = pipeline
    output = suggestions(source, actor)
    output["suggestions"][0]["evidence"][0]["source_id"] = canonical_hash("foreign")
    with pytest.raises(ValueError, match="outside"):
        await extract(pipeline, output)
    with database.session_factory() as session:
        assert list(session.scalars(select(MemoryExtractionRow))) == []


def ready_consolidation(pipeline):
    """Make a queued owner wakeup due without sleeping through its debounce interval."""
    database, outbox, _, _, _, actor, _ = pipeline
    with database.session_factory.begin() as session:
        owner = session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id))
        session.get(
            OutboxEventRow, owner.active_consolidation_event_id
        ).available_at = datetime.now(UTC)
    return outbox.claim_pending(destination="memory_consolidate", limit=1)[0]


def add_ready_input(pipeline, *, closure="new-input"):
    """Simulate a second atomic part closure while the first owner job is running."""
    database, _, model, _, _, actor, source = pipeline
    with database.session_factory.begin() as session:
        from financeclaw.shared.memory.repository import lock_owner

        owner = lock_owner(session, actor)
        owner.extraction_revision += 1
        row = MemoryExtractionRow(
            extraction_id=closure,
            tenant_id=actor.tenant_id,
            subject_id=actor.subject_id,
            closure_hash=canonical_hash(closure),
            prepare_event_id="prepare",
            part_index=0,
            part_count=1,
            pipeline_version="memory/1",
            model_profile_version=model.fingerprint,
            schema_version="memory-v1",
            evidence_source_ids=[source.source_id],
            evidence=[source.evidence_ref().model_dump(mode="json")],
            output={"suggestions": []},
            coverage={"complete": True},
            extraction_revision=owner.extraction_revision,
        )
        session.add(row)
        session.flush()
        ensure_consolidation_in_session(session, owner, model_profile_version=model.fingerprint)


@pytest.mark.asyncio
async def test_new_ready_input_during_commit_gets_successor_without_lost_wakeup(pipeline):
    """S21: input arriving after the model snapshot survives old event completion."""
    database, outbox, model, _, _, actor, _ = pipeline
    await extract(pipeline, {"suggestions": []})
    event = ready_consolidation(pipeline)
    handler = ConsolidationHandler(database.session_factory, outbox, model)
    snapshot = handler._snapshot(event)
    add_ready_input(pipeline)
    from financeclaw.memory_worker.prompts import MemorySuggestions

    handler._commit(event, snapshot, MemorySuggestions())
    with database.session_factory() as session:
        owner = session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id))
        assert owner.active_consolidation_event_id not in {None, event.event_id}
        assert owner.consolidated_revision == 1
        assert session.get(MemoryExtractionRow, "new-input").disposition == "pending"
        assert session.get(OutboxEventRow, event.event_id).status == "published"


@pytest.mark.asyncio
async def test_owner_change_rejects_whole_old_consolidation_snapshot(pipeline):
    """S20: an intervening user action prevents every stale model mutation and consumption."""
    database, outbox, model, _, _, actor, source = pipeline
    await extract(pipeline, suggestions(source, actor))
    event = ready_consolidation(pipeline)
    handler = ConsolidationHandler(database.session_factory, outbox, model)
    snapshot = handler._snapshot(event)
    MemoryMutationService(database.session_factory).apply(
        actor,
        MemoryMutation(
            mutation_id="user-correction",
            content="用户明确保存的新事实",
            explicit_intent=True,
        ),
    )
    from financeclaw.memory_worker.prompts import MemorySuggestions

    with pytest.raises(MemoryConflict, match="changed"):
        handler._commit(
            event, snapshot, MemorySuggestions.model_validate(suggestions(source, actor))
        )
    with database.session_factory() as session:
        assert session.scalar(select(MemoryExtractionRow)).disposition == "pending"
        assert len(list(session.scalars(select(MemoryRecordRow)))) == 1


@pytest.mark.asyncio
async def test_dead_input_quarantined_without_refreshing_budget_or_blocking_new_input(pipeline):
    """S53: terminal failure releases the active pointer and isolates only its snapshot IDs."""
    database, outbox, model, _, _, actor, _ = pipeline
    await extract(pipeline, {"suggestions": []})
    event = ready_consolidation(pipeline)
    handler = ConsolidationHandler(database.session_factory, outbox, model)
    snapshot = handler._snapshot(event)
    add_ready_input(pipeline)
    handler.fail(event, "ModelBudgetExhausted", terminal=True)
    with database.session_factory() as session:
        owner = session.get(MemoryOwnerRow, (actor.tenant_id, actor.subject_id))
        assert owner.active_consolidation_event_id not in {None, event.event_id}
        assert session.get(OutboxEventRow, event.event_id).status == "dead_letter"
        assert (
            session.get(MemoryExtractionRow, snapshot.extraction_ids[0]).disposition
            == "quarantined"
        )
        assert session.get(MemoryExtractionRow, "new-input").disposition == "pending"
