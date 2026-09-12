"""Original evidence retention and management filtering remain exact after Turn completion."""

from datetime import UTC, datetime, timedelta

import pytest

from financeclaw.shared.artifacts.repository import ArtifactNotFound, SqlAlchemyArtifactRepository
from financeclaw.shared.artifacts.service import ArtifactService
from financeclaw.shared.artifacts.storage import InMemoryArtifactStore
from financeclaw.shared.artifacts.tables import ArtifactMetadataRow
from financeclaw.shared.conversation.lifecycle import ConversationRetention, responsibility_reasons
from financeclaw.shared.memory.models import MemoryMutation
from financeclaw.shared.memory.tables import MemoryExtractionRow, MemoryRecordRow, MemorySourceRow
from tests.stage11.domain_support import add_user_source, create_domain
from tests.turn_support import finish_turn


@pytest.fixture
def domain(tmp_path):
    """Use an isolated SQL authority and in-memory artifact backend for retention checks."""
    stack = create_domain(tmp_path)
    yield stack
    stack[0].close()


def _reasons(database, context):
    """Read the same protection snapshot used by maintenance and artifact reads."""
    with database.session_factory() as session:
        return responsibility_reasons(session, context.conversation_id)


def test_completed_source_protects_expired_artifacts_until_derivation_is_disposed(domain):
    """S35: settling the business Turn cannot remove original material still needed by memory."""
    database, conversations, _, _, _ = domain
    _, source, context = add_user_source(domain, "研究债券的期限风险")
    context = context.model_copy(update={"scopes": {*context.scopes, "artifacts:read"}})
    store = InMemoryArtifactStore()
    artifacts = ArtifactService(SqlAlchemyArtifactRepository(database.session_factory), store)
    artifact = artifacts.persist(
        "原始研究附件",
        context=context,
        source_type="tool_result",
        source_id="call",
        idempotency_key="a",
    )
    finish_turn(conversations, context.turn_id)
    with database.session_factory.begin() as session:
        session.get(ArtifactMetadataRow, artifact.artifact_id).expires_at = datetime.now(
            UTC
        ) - timedelta(days=1)
    retention = ConversationRetention(database.session_factory, store)
    assert _reasons(database, context) == ("memory_source_unprocessed",)
    assert retention.cleanup_artifacts(apply=True)[0]["status"] == "protected"
    assert artifacts.read(artifact.artifact_id, context=context) == "原始研究附件".encode()
    with database.session_factory.begin() as session:
        session.add(
            MemoryExtractionRow(
                extraction_id="disposed",
                tenant_id=source.tenant_id,
                subject_id=source.subject_id,
                closure_hash="a" * 64,
                prepare_event_id="prepare",
                part_index=0,
                part_count=1,
                pipeline_version="memory/1",
                model_profile_version="offline",
                evidence_source_ids=[source.source_id],
                disposition="consumed",
            )
        )
    assert _reasons(database, context) == ()
    with pytest.raises(ArtifactNotFound, match="expired"):
        artifacts.read(artifact.artifact_id, context=context)
    assert retention.cleanup_artifacts(apply=True)[0]["status"] == "deleted"


def test_pending_output_and_unexpired_candidate_protect_independently(domain):
    """S35: revoking derivation does not release pending outputs or candidate review evidence."""
    database, conversations, _, service, repository = domain
    actor, source, context = add_user_source(domain, "我有保守的投资目标")
    proposal = service.apply(
        actor,
        MemoryMutation(
            mutation_id="risk",
            kind="profile",
            field="risk_statement",
            content="风险承受能力低",
            evidence=(source.evidence_ref(),),
        ),
    )
    finish_turn(conversations, context.turn_id)
    with database.session_factory.begin() as session:
        session.get(MemorySourceRow, source.source_id).permit_revoked = True
        session.add(
            MemoryExtractionRow(
                extraction_id="pending",
                tenant_id=actor.tenant_id,
                subject_id=actor.subject_id,
                closure_hash="b" * 64,
                prepare_event_id="prepare",
                part_index=0,
                part_count=1,
                pipeline_version="memory/1",
                model_profile_version="offline",
                evidence_source_ids=[source.source_id],
                disposition="pending",
            )
        )
    assert _reasons(database, context) == ("memory_extraction_output", "memory_candidate")
    with database.session_factory.begin() as session:
        session.get(MemoryExtractionRow, "pending").disposition = "quarantined"
    assert _reasons(database, context) == ("memory_candidate",)
    with database.session_factory.begin() as session:
        session.get(MemoryRecordRow, (proposal.candidate_id, 1)).expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
    assert _reasons(database, context) == ()
    records, cursor = repository.list_page(actor, status="expired", kind="profile")
    assert cursor is None and [record.status for record in records] == ["expired"]
    assert repository.list_page(actor, status="proposed")[0] == ()


def test_management_scope_filters_and_empty_page_preserve_keyset_progress(domain):
    """A filtered invalid source page must not prevent reaching later valid records."""
    database, _, actor, service, repository = domain
    for number in range(3):
        service.apply(
            actor,
            MemoryMutation(
                mutation_id=f"task-{number}",
                kind="task",
                content=f"已完成研究{number}",
                scope_type="conversation",
                scope_id="target" if number != 2 else "another",
                explicit_intent=True,
            ),
        )
    filtered, _ = repository.list_page(actor, scope_type="conversation", scope_id="target")
    assert len(filtered) == 2
    assert repository.list_page(actor, scope_type="agent")[0] == ()
    first, second = sorted(filtered, key=lambda record: record.memory_id)
    with database.session_factory.begin() as session:
        session.get(MemorySourceRow, first.evidence[0].source_id).visible = False
    records, cursor = repository.list_page(
        actor, scope_type="conversation", scope_id="target", limit=1
    )
    assert records == () and cursor == first.memory_id
    records, cursor = repository.list_page(
        actor, scope_type="conversation", scope_id="target", limit=1, after=cursor
    )
    assert [record.memory_id for record in records] == [second.memory_id] and cursor is None
