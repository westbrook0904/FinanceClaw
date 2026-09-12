"""SQL fact/audit atomicity replaces the former Store receipt-pending recovery protocol."""

import pytest
from sqlalchemy import func, select

from financeclaw.shared.audit.tables import AuditRecordRow
from financeclaw.shared.memory.models import MemoryMutation, MemoryNotFound
from financeclaw.shared.memory.tables import MemoryRecordRow
from tests.stage11.domain_support import create_domain


def test_replacement_audit_failure_rolls_back_the_entire_mutation(tmp_path, monkeypatch):
    """A failed audit cannot leave new fact text visible or an old fact partly superseded."""
    database, _, actor, service, repository = create_domain(tmp_path)
    try:
        first = service.apply(
            actor, MemoryMutation(mutation_id="old", content="原计划", explicit_intent=True)
        )
        replacement = MemoryMutation(
            mutation_id="new",
            operation="update",
            memory_id=first.memory_id,
            expected_revision=1,
            content="新计划",
            explicit_intent=True,
        )
        append = service.audit.append_in_session

        def fail(*args):
            """Fail exactly at the permanent receipt append boundary."""
            raise ConnectionError("audit fault")

        monkeypatch.setattr(service.audit, "append_in_session", fail)
        with pytest.raises(ConnectionError):
            service.apply(actor, replacement)
        assert repository.get(actor, first.memory_id).content == "原计划"
        monkeypatch.setattr(service.audit, "append_in_session", append)
        new = service.apply(actor, replacement)
        replay = service.apply(actor, replacement)
        assert replay.replayed and replay.revision == new.revision == 2
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AuditRecordRow)) == 2
    finally:
        database.close()


def test_forget_receipt_survives_uncertain_client_commit_and_reentry(tmp_path):
    """A lost successful response never causes a second privacy transition or deletion write."""
    database, _, actor, service, repository = create_domain(tmp_path)
    try:
        saved = service.apply(
            actor, MemoryMutation(mutation_id="save", content="待遗忘的任务", explicit_intent=True)
        )
        forget = MemoryMutation(
            mutation_id="forget",
            operation="forget",
            memory_id=saved.memory_id,
            expected_revision=1,
            explicit_intent=True,
        )
        service.apply(actor, forget)  # Simulate losing the successful client response.
        first_snapshot = repository.owner_snapshot(actor)
        replay = service.apply(actor, forget)
        assert replay.replayed and replay.status == "forgotten"
        assert repository.owner_snapshot(actor) == first_snapshot
        with pytest.raises(MemoryNotFound):
            repository.get(actor, saved.memory_id)
        with database.session_factory() as session:
            assert all(not body for body in session.scalars(select(MemoryRecordRow.content)))
            assert session.scalar(select(func.count()).select_from(AuditRecordRow)) == 2
    finally:
        database.close()
