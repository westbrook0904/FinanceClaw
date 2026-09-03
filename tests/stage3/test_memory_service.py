import os
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import PostgresStore
from pydantic import ValidationError

from financeclaw.audit import AuditEventType, InMemoryAuditRepository
from financeclaw.contracts import ExecutionContext
from financeclaw.memory import (
    LongTermMemoryService,
    MemoryConfirmationRequired,
    MemoryDraft,
    MemoryPolicy,
    MemoryPolicyViolation,
    MemoryStatus,
)

from .support import conversation_context, journal


def test_draft_shape_and_financial_fact_boundary() -> None:
    with pytest.raises(ValidationError):
        MemoryDraft.model_validate(
            {
                "kind": "preference",
                "content": "偏好低波动资产",
                "evidence_message_ids": ["current"],
                "tenant_id": "model-forged-tenant",
            }
        )
    with pytest.raises(ValidationError):
        MemoryDraft(
            kind="domain_fact",
            content="AAPL is attractive",
            evidence_message_ids=("current",),
        )
    policy = MemoryPolicy()
    with pytest.raises(MemoryPolicyViolation, match="financial facts"):
        policy.assess(
            MemoryDraft(
                kind="decision_note",
                content="AAPL current price is 250 USD",
                evidence_message_ids=("current",),
            )
        )
    with pytest.raises(MemoryPolicyViolation, match="credentials"):
        policy.assess(
            MemoryDraft(
                kind="constraint",
                content="API key: sk-abcdefghijklmnop",
                evidence_message_ids=("current",),
            )
        )


def test_trusted_namespace_evidence_lifecycle_and_cross_thread_recall(tmp_path: Path) -> None:
    database, repository = journal(tmp_path / "memory.db")
    audit = InMemoryAuditRepository()
    service = LongTermMemoryService(
        conversation_repository=repository,
        audit=audit,
    )
    store = InMemoryStore()
    first_context, first_message_id = conversation_context(repository)
    draft = MemoryDraft(
        kind="preference",
        content="用户偏好低波动资产",
        evidence_message_ids=("current",),
    )
    proposal = service.propose(first_context, draft)
    assert proposal.draft.evidence_message_ids == (first_message_id,)
    assert first_context.tenant_id not in service.namespace(first_context)
    assert first_context.subject_id not in service.namespace(first_context)

    with pytest.raises(MemoryConfirmationRequired):
        service.confirm(
            first_context,
            store,
            proposal_id=proposal.proposal_id,
            draft=proposal.draft,
            user_confirmed=False,
        )
    first = service.confirm(
        first_context,
        store,
        proposal_id=proposal.proposal_id,
        draft=proposal.draft,
        user_confirmed=True,
    )
    assert first.status is MemoryStatus.ACTIVE
    assert first.namespace == service.namespace(first_context)

    # A new conversation for the same authenticated subject resolves the same
    # Store namespace, while a forged tenant/subject cannot address it.
    second_context, second_message_id = conversation_context(
        repository,
        message="把风险偏好改成只投资现金类资产",
        key="replacement-turn",
    )
    recalls = service.search(
        second_context,
        store,
        query="低波动",
        for_model_context=True,
    )
    assert tuple(item.record.memory_id for item in recalls) == (first.memory_id,)
    other_subject = second_context.model_copy(
        update={"subject_id": "subject.b", "conversation_id": None}
    )
    assert service.search(other_subject, store, query="低波动") == ()

    replacement_proposal = service.propose(
        second_context,
        MemoryDraft(
            kind="preference",
            content="用户只投资现金类资产",
            evidence_message_ids=(second_message_id,),
        ),
    )
    replacement = service.confirm(
        second_context,
        store,
        proposal_id=replacement_proposal.proposal_id,
        draft=replacement_proposal.draft,
        user_confirmed=True,
        supersedes_id=first.memory_id,
    )
    assert replacement.supersedes_id == first.memory_id
    assert service.get(second_context, store, first.memory_id) is None
    assert (
        service.get(second_context, store, first.memory_id, include_inactive=True).status
        is MemoryStatus.SUPERSEDED
    )

    revoked = service.forget(second_context, store, replacement.memory_id, mode="revoke")
    assert revoked.status is MemoryStatus.REVOKED
    assert service.search(second_context, store, query=None) == ()
    deleted = service.forget(second_context, store, replacement.memory_id, mode="delete")
    assert deleted.status is MemoryStatus.DELETED
    assert service.get(second_context, store, replacement.memory_id) is None

    memory_events = [
        record.event_type for record in audit.records() if record.resource_type == "memory"
    ]
    assert memory_events == [
        AuditEventType.MEMORY_PROPOSED,
        AuditEventType.MEMORY_COMMITTED,
        AuditEventType.MEMORY_PROPOSED,
        AuditEventType.MEMORY_COMMITTED,
        AuditEventType.MEMORY_SUPERSEDED,
        AuditEventType.MEMORY_REVOKED,
        AuditEventType.MEMORY_DELETED,
    ]
    database.close()


def test_evidence_cannot_cross_conversation_or_owner(tmp_path: Path) -> None:
    database, repository = journal(tmp_path / "evidence.db")
    context_a, message_a = conversation_context(repository, key="owner-a")
    context_b, _ = conversation_context(
        repository,
        subject_id="subject.b",
        message="另一个用户的偏好",
        key="owner-b",
    )
    service = LongTermMemoryService(
        conversation_repository=repository,
        audit=InMemoryAuditRepository(),
    )
    with pytest.raises(RuntimeError, match="evidence"):
        service.propose(
            context_b,
            MemoryDraft(
                kind="preference",
                content="试图引用另一个用户",
                evidence_message_ids=(message_a,),
            ),
        )
    forged_context = ExecutionContext(
        tenant_id="tenant.b",
        subject_id=context_a.subject_id,
        conversation_id=context_a.conversation_id,
        turn_id=context_a.turn_id,
        run_id=context_a.run_id,
    )
    with pytest.raises(LookupError):
        service.propose(
            forged_context,
            MemoryDraft(
                kind="preference",
                content="试图伪造租户",
                evidence_message_ids=(message_a,),
            ),
        )
    database.close()


def test_store_value_cannot_forge_identity_inside_trusted_namespace(tmp_path: Path) -> None:
    database, repository = journal(tmp_path / "corrupt-store.db")
    context, _ = conversation_context(repository, key="corrupt-record")
    service = LongTermMemoryService(
        conversation_repository=repository,
        audit=InMemoryAuditRepository(),
    )
    store = InMemoryStore()
    proposal = service.propose(
        context,
        MemoryDraft(
            kind="constraint",
            content="用户不使用杠杆",
            evidence_message_ids=("current",),
        ),
    )
    record = service.confirm(
        context,
        store,
        proposal_id=proposal.proposal_id,
        draft=proposal.draft,
        user_confirmed=True,
    )
    forged = record.model_copy(update={"tenant_id": "tenant.b"})
    store.put(
        service.namespace(context),
        forged.memory_id,
        forged.model_dump(mode="json"),
        index=False,
    )
    with pytest.raises(RuntimeError, match="identity"):
        service.search(context, store)
    database.close()


@pytest.mark.external
@pytest.mark.skipif(
    not os.getenv("FINANCECLAW_SPIKE_POSTGRES_DSN"),
    reason="PostgreSQL Store probe setting is not configured",
)
def test_postgres_store_record_survives_connection_reconstruction(tmp_path: Path) -> None:
    """Exercise the real Store adapter twice to model a worker restart."""

    database, repository = journal(tmp_path / "postgres-store.db")
    unique = uuid4().hex
    context, _ = conversation_context(
        repository,
        tenant_id=f"stage3-{unique}",
        subject_id=f"stage3-{unique}",
        key=f"postgres-{unique}",
    )
    service = LongTermMemoryService(
        conversation_repository=repository,
        audit=InMemoryAuditRepository(),
    )
    draft = MemoryDraft(
        kind="goal",
        content="用户希望建立长期低波动组合",
        evidence_message_ids=("current",),
    )
    proposal = service.propose(context, draft)
    dsn = os.environ["FINANCECLAW_SPIKE_POSTGRES_DSN"]
    with PostgresStore.from_conn_string(dsn) as first_store:
        first_store.setup()
        committed = service.confirm(
            context,
            first_store,
            proposal_id=proposal.proposal_id,
            draft=proposal.draft,
            user_confirmed=True,
        )
    with PostgresStore.from_conn_string(dsn) as restarted_store:
        restored = service.get(context, restarted_store, committed.memory_id)
        assert restored == committed
        # The test owns this unique namespace and removes its fixture after
        # proving persistence so shared development databases do not grow.
        restarted_store.delete(service.namespace(context), committed.memory_id)
    database.close()
