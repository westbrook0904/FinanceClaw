"""Memory ownership and source governance regressions on the Stage 11 SQL authority."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from financeclaw.shared.memory.evidence import EvidenceReader
from financeclaw.shared.memory.models import MemoryMutation, MemoryNotFound, MemoryPermissionError
from financeclaw.shared.memory.namespace import memory_index_namespace
from financeclaw.shared.memory.repository import MemoryRepository
from financeclaw.shared.memory.tables import MemoryRecordRow
from tests.stage11.domain_support import add_user_source, create_domain


def test_model_cannot_supply_identity_or_unregistered_fact_kind():
    """Reject owner/approval injection and unsupported model-generated record types."""
    with pytest.raises(ValidationError):
        MemoryMutation.model_validate(
            {"mutation_id": "x", "content": "fact", "tenant_id": "forged"}
        )
    with pytest.raises(ValidationError):
        MemoryMutation(mutation_id="x", content="fact", kind="domain_fact")
    with pytest.raises(ValidationError):
        MemoryMutation.model_validate({"mutation_id": "x", "content": "fact", "approved": True})


def test_source_hash_span_and_owner_are_verified_against_original_journal(tmp_path):
    """Only exact original source versions can support a model memory proposal."""
    stack = create_domain(tmp_path)
    database = stack[0]
    try:
        actor, source, _ = add_user_source(stack, "以后都用中文回答")
        reference = source.evidence_ref()
        with database.session_factory() as session:
            document = EvidenceReader().read_in_session(session, actor, (reference,))[0]
            assert document.content == "以后都用中文回答"
            with pytest.raises(MemoryPermissionError, match="hash"):
                EvidenceReader().read_in_session(
                    session, actor, (reference.model_copy(update={"content_hash": "f" * 64}),)
                )
            with pytest.raises(MemoryPermissionError, match="span"):
                EvidenceReader().read_in_session(
                    session, actor, (reference.model_copy(update={"span": (0, 9999)}),)
                )
            with pytest.raises(MemoryNotFound):
                EvidenceReader().read_in_session(
                    session, actor.model_copy(update={"subject_id": "other"}), (reference,)
                )
    finally:
        database.close()


def test_memory_survives_session_reconstruction_and_never_crosses_owner(tmp_path):
    """Facts survive reader reconstruction; SQL owner filters precede body access."""
    database, _, actor, service, _ = create_domain(tmp_path)
    try:
        saved = service.apply(
            actor,
            MemoryMutation(
                mutation_id="task", content="2026-09-12 完成住房方案比较", explicit_intent=True
            ),
        )
        repository = MemoryRepository(database.session_factory)
        assert repository.get(actor, saved.memory_id).content.endswith("住房方案比较")
        with pytest.raises(MemoryNotFound):
            repository.get(actor.model_copy(update={"tenant_id": "foreign"}), saved.memory_id)
        assert repository.list_records(actor.model_copy(update={"subject_id": "foreign"})) == ()
    finally:
        database.close()


def test_index_namespace_has_exact_encoded_owner_and_version(tmp_path):
    """Store identity is derived from trusted owner values and versioned separately from facts."""
    database, _, actor, _, _ = create_domain(tmp_path)
    try:
        namespace = memory_index_namespace(actor)
        assert namespace[:2] == ("financeclaw", "v3")
        assert namespace[-2:] == ("memory_index", "memory-v1")
        assert actor.tenant_id not in namespace and actor.subject_id not in namespace
        assert namespace != memory_index_namespace(actor.model_copy(update={"subject_id": "other"}))
    finally:
        database.close()


def test_expired_and_foreign_scope_records_do_not_return_body(tmp_path):
    """Cached exact versions still obey expiry and the currently trusted execution scope."""
    database, _, actor, service, repository = create_domain(tmp_path)
    try:
        saved = service.apply(
            actor,
            MemoryMutation(
                mutation_id="scoped",
                content="有限会话任务",
                scope_type="conversation",
                scope_id="conversation-one",
                explicit_intent=True,
            ),
        )
        with pytest.raises(MemoryNotFound):
            repository.get(
                actor.model_copy(update={"kind": "tool", "conversation_id": "conversation-two"}),
                saved.memory_id,
            )
        with database.session_factory.begin() as session:
            session.get(MemoryRecordRow, (saved.memory_id, 1)).expires_at = datetime.now(
                UTC
            ) - timedelta(seconds=1)
        with pytest.raises(MemoryNotFound):
            repository.get(actor, saved.memory_id, revision=1)
    finally:
        database.close()
