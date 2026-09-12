"""Stage 11 transaction, evidence, confirmation and privacy regression contracts."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select

from financeclaw.shared.audit.models import AuditEventType, AuditRecord
from financeclaw.shared.memory.evidence import EvidenceReader
from financeclaw.shared.memory.lifecycle import (
    invalidate_sources_in_session,
    revoke_turn_sources_in_session,
)
from financeclaw.shared.memory.models import (
    MemoryConflict,
    MemoryMutation,
    MemoryNotFound,
    MemoryPermissionError,
)
from financeclaw.shared.memory.repository import lock_owner
from financeclaw.shared.memory.tables import MemoryRecordRow, MemorySourceRow
from financeclaw.shared.outbox.tables import OutboxEventRow
from tests.stage11.domain_support import add_user_source, create_domain


@pytest.fixture
def domain(tmp_path):
    """Dispose all temporary SQL resources after each independent scenario."""
    stack = create_domain(tmp_path)
    yield stack
    stack[0].close()


def language(mutation_id, content="zh-CN", **changes):
    """Build an explicit management request for a low-risk registered field."""
    return MemoryMutation(
        mutation_id=mutation_id,
        kind="profile",
        field="language",
        content=content,
        explicit_intent=True,
        **changes,
    )


def test_old_mutation_does_not_revert_new_profile_after_outbox_cleanup(domain):
    """S13: permanent SQL receipt defeats historical write replay after event retention."""
    database, _, actor, service, repository = domain
    original = language("a")
    first = service.apply(actor, original)
    second = service.apply(
        actor,
        language(
            "b",
            "en",
            operation="update",
            memory_id=first.memory_id,
            expected_revision=first.revision,
        ),
    )
    with database.session_factory.begin() as session:
        session.execute(delete(OutboxEventRow))
    replay = service.apply(actor, original)
    assert replay.replayed and replay.revision == first.revision
    assert repository.get(actor, first.memory_id).content == "en"
    assert repository.owner_snapshot(actor).memory_revision == second.owner_revision
    assert repository.get(actor, first.memory_id, revision=first.revision).content == "zh-CN"


def test_reused_mutation_with_changed_input_conflicts(domain):
    """S14: a stable request identity cannot be used to submit a different fact."""
    _, _, actor, service, _ = domain
    service.apply(actor, language("a"))
    with pytest.raises(MemoryConflict, match="different operation"):
        service.apply(actor, language("a", "en"))


def test_audit_failure_rolls_back_fact_source_and_revision(domain, monkeypatch):
    """SQL authority and permanent receipt cannot diverge when auditing fails."""
    database, _, actor, service, repository = domain

    def fail(*args):
        """Inject a failure inside the shared SQL write transaction."""
        raise ConnectionError("audit fault")

    monkeypatch.setattr(service.audit, "append_in_session", fail)
    with pytest.raises(ConnectionError):
        service.apply(actor, language("a"))
    assert repository.owner_snapshot(actor).memory_revision == 0
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MemoryRecordRow)) == 0
        assert session.scalar(select(func.count()).select_from(MemorySourceRow)) == 0


def test_high_impact_candidates_require_independent_authenticated_decision(domain):
    """S05/S26: API and model proposals both need a separate reviewed candidate decision."""
    _, _, actor, service, repository = domain
    proposed = service.apply(
        actor,
        MemoryMutation(
            mutation_id="risk",
            kind="profile",
            field="risk_statement",
            content="低风险承受能力",
            explicit_intent=True,
        ),
    )
    assert proposed.status == "proposed"
    assert repository.list_records(actor) == ()
    candidate = repository.get(actor, proposed.candidate_id, include_candidates=True)
    worker = actor.model_copy(update={"kind": "worker"})
    with pytest.raises(MemoryPermissionError):
        service.decide(
            worker,
            candidate.memory_id,
            "approve",
            "decision",
            candidate.revision,
            candidate.content_hash,
        )
    committed = service.decide(
        actor,
        candidate.memory_id,
        "approve",
        "decision",
        candidate.revision,
        candidate.content_hash,
    )
    assert repository.get(actor, committed.memory_id).content == candidate.content


def test_candidate_receipt_replays_after_expiry_but_opposite_decision_conflicts(domain):
    """S27: replay authorization precedes current expiry checks without reopening decisions."""
    database, _, actor, service, repository = domain
    proposal = service.apply(
        actor,
        MemoryMutation(
            mutation_id="risk",
            kind="profile",
            field="risk_statement",
            content="谨慎",
            explicit_intent=True,
        ),
    )
    candidate = repository.get(actor, proposal.candidate_id, include_candidates=True)
    receipt = service.decide(
        actor,
        candidate.memory_id,
        "approve",
        "decision",
        candidate.revision,
        candidate.content_hash,
    )
    with database.session_factory.begin() as session:
        session.get(MemoryRecordRow, (candidate.memory_id, 2)).expires_at = datetime.now(
            UTC
        ) - timedelta(days=1)
    replay = service.decide(
        actor,
        candidate.memory_id,
        "approve",
        "decision",
        candidate.revision,
        candidate.content_hash,
    )
    assert replay.replayed and replay.memory_id == receipt.memory_id
    with pytest.raises(MemoryConflict):
        service.decide(
            actor,
            candidate.memory_id,
            "reject",
            "opposite",
            candidate.revision,
            candidate.content_hash,
        )


def test_candidate_does_not_override_a_concurrent_edit(domain):
    """S28: approving an old candidate cannot silently adopt a newer target revision."""
    _, _, actor, service, repository = domain
    first = service.apply(actor, language("language"))
    proposal = service.apply(
        actor,
        language(
            "proposal",
            "en",
            operation="update",
            memory_id=first.memory_id,
            expected_revision=first.revision,
        ).model_copy(update={"explicit_intent": False}),
    )
    candidate = repository.get(actor, proposal.candidate_id, include_candidates=True)
    service.apply(
        actor,
        language(
            "correction",
            "en",
            operation="update",
            memory_id=first.memory_id,
            expected_revision=first.revision,
        ),
    )
    with pytest.raises(MemoryConflict, match="revision"):
        service.decide(
            actor,
            candidate.memory_id,
            "approve",
            "approval",
            candidate.revision,
            candidate.content_hash,
        )


def test_forget_erases_all_fact_versions_and_replay_never_returns_body(domain):
    """S32/S33: forget is immediate SQL privacy enforcement with asynchronous purge intent."""
    database, _, actor, service, repository = domain
    first = service.apply(actor, language("original"))
    old = repository.get(actor, first.memory_id)
    second = service.apply(
        actor,
        language("new", "en", operation="update", memory_id=first.memory_id, expected_revision=1),
    )
    receipt = service.apply(
        actor,
        MemoryMutation(
            mutation_id="forget",
            operation="forget",
            memory_id=first.memory_id,
            expected_revision=second.revision,
            explicit_intent=True,
        ),
    )
    assert (
        receipt.status == "forgotten"
        and receipt.privacy_epoch == 1
        and receipt.purge_status == "pending"
    )
    with pytest.raises(MemoryNotFound):
        repository.get(actor, first.memory_id, revision=1)
    with database.session_factory() as session:
        assert all(not text for text in session.scalars(select(MemoryRecordRow.content)))
        assert session.scalar(select(func.count()).select_from(OutboxEventRow)) >= 2
    assert "content" not in service.apply(actor, language("original")).model_dump()
    with pytest.raises((MemoryPermissionError, MemoryNotFound)):
        service.apply(
            actor.model_copy(update={"kind": "tool"}),
            language("resurrect").model_copy(update={"evidence": old.evidence}),
        )


def test_new_explicit_source_can_remember_after_forget(domain):
    """S34: fresh user authority can create a later revision beyond a forget cutoff."""
    _, _, actor, service, repository = domain
    first = service.apply(actor, language("a"))
    service.apply(
        actor,
        MemoryMutation(
            mutation_id="forget",
            operation="forget",
            memory_id=first.memory_id,
            expected_revision=1,
            explicit_intent=True,
        ),
    )
    again = service.apply(actor, language("new-source", "en"))
    assert again.revision == 3
    assert repository.get(actor, again.memory_id).content == "en"


def test_model_forget_without_exact_intent_only_proposes(domain):
    """S52: tool permission alone is not authority for an unrequested memory deletion."""
    _, _, actor, service, repository = domain
    first = service.apply(actor, language("a"))
    tool_actor, source, _ = add_user_source(domain, "继续分析市场", "unrelated")
    proposal = service.apply(
        tool_actor.model_copy(update={"kind": "tool"}),
        MemoryMutation(
            mutation_id="tool-forget",
            operation="forget",
            memory_id=first.memory_id,
            expected_revision=1,
            evidence=(source.evidence_ref(),),
        ),
    )
    assert proposal.status == "proposed"
    candidate = repository.get(actor, proposal.candidate_id, include_candidates=True)
    assert candidate.content == "zh-CN" and candidate.operation == "forget"
    no_delete = actor.model_copy(update={"scopes": frozenset({"memory:write"})})
    with pytest.raises(MemoryPermissionError, match="memory:delete"):
        service.decide(
            no_delete,
            candidate.memory_id,
            "approve",
            "approve",
            candidate.revision,
            candidate.content_hash,
        )


def test_tool_exact_original_forget_command_can_commit(domain):
    """An exact original target-bearing command is verifiable without a model approval flag."""
    _, _, actor, service, _ = domain
    first = service.apply(actor, language("a"))
    tool_actor, source, _ = add_user_source(
        domain, f"请删除记忆 {first.memory_id}", "forget-command"
    )
    receipt = service.apply(
        tool_actor.model_copy(update={"kind": "tool"}),
        MemoryMutation(
            mutation_id="tool-forget",
            operation="forget",
            memory_id=first.memory_id,
            expected_revision=1,
            evidence=(source.evidence_ref(),),
        ),
    )
    assert receipt.status == "forgotten"


@pytest.mark.parametrize(
    "content", ["API key: sk-abcdefghijklmnop", "AAPL current price is 250 USD"]
)
def test_credential_and_current_financial_content_cannot_be_saved(domain, content):
    """Live balances/prices and secrets never become memory, even through user management."""
    _, _, actor, service, _ = domain
    with pytest.raises(MemoryPermissionError):
        service.apply(
            actor, MemoryMutation(mutation_id="unsafe", content=content, explicit_intent=True)
        )


@pytest.mark.parametrize(
    "text", ["这次用中文", "假设我以后用中文", "他说以后用中文", "以后不要记住中文偏好"]
)
def test_nonasserted_profile_is_not_even_promoted_to_candidate(domain, text):
    """S03: quote, hypothesis and temporary instructions remain task context only."""
    actor, source, _ = add_user_source(domain, text)
    with pytest.raises(MemoryPermissionError):
        domain[3].apply(
            actor.model_copy(update={"kind": "tool"}),
            language("inferred").model_copy(
                update={"evidence": (source.evidence_ref(),), "explicit_intent": False}
            ),
        )


def test_foreign_owner_evidence_and_memory_are_rejected(domain):
    """S51: restrict both fact and source ownership before selecting any body."""
    _, _, actor, service, repository = domain
    saved = service.apply(actor, language("a"))
    other = actor.model_copy(update={"subject_id": "foreign"})
    with pytest.raises(MemoryNotFound):
        repository.get(other, saved.memory_id)
    evidence = repository.get(actor, saved.memory_id).evidence
    with pytest.raises(MemoryNotFound):
        service.apply(
            other.model_copy(update={"kind": "tool"}),
            MemoryMutation(mutation_id="forge", content="forged", evidence=evidence),
        )


def test_derive_permit_survives_turn_expiry_but_not_explicit_revocation(domain):
    """S37: finite derive authority is independent of the natural Turn grant deadline."""
    from financeclaw.shared.turns.tables import ConversationTurnRow

    database = domain[0]
    actor, source, _ = add_user_source(domain, "研究住房规划")
    worker = actor.model_copy(update={"kind": "worker", "permit_source_ids": (source.source_id,)})
    with database.session_factory.begin() as session:
        session.get(ConversationTurnRow, actor.turn_id).grant_expires_at = datetime.now(
            UTC
        ) - timedelta(hours=1)
        assert EvidenceReader().read_in_session(
            session, worker, (source.evidence_ref(),), for_derivation=True
        )
        revoke_turn_sources_in_session(session, actor, actor.turn_id)
    with database.session_factory() as session, pytest.raises(MemoryPermissionError):
        EvidenceReader().read_in_session(
            session, worker, (source.evidence_ref(),), for_derivation=True
        )


def test_source_invalidation_suspends_fact_and_clears_original_projection(domain):
    """S35: hidden evidence invalidates the complete dependent fact, not only its reference."""
    database, _, _, service, repository = domain
    actor, source, _ = add_user_source(domain, "记录有日期的研究任务")
    saved = service.apply(
        actor.model_copy(update={"kind": "tool"}),
        MemoryMutation(
            mutation_id="task", content="研究任务已完成", evidence=(source.evidence_ref(),)
        ),
    )
    with database.session_factory.begin() as session:
        invalidate_sources_in_session(
            session, actor, lock_owner(session, actor), {source.source_id}
        )
    with pytest.raises(MemoryNotFound):
        repository.get(actor, saved.memory_id)
    assert repository.owner_snapshot(actor).privacy_epoch == 1


def test_settings_use_current_scope_and_permanent_mutation_receipt(domain):
    """Policy updates do not regain old model authority when retried after later changes."""
    _, _, actor, service, repository = domain
    first = service.update_settings(
        actor, mutation_id="disable", expected_policy_revision=1, auto_enabled=False
    )
    second = service.update_settings(
        actor, mutation_id="enable", expected_policy_revision=2, auto_enabled=True
    )
    assert (
        service.update_settings(
            actor, mutation_id="disable", expected_policy_revision=1, auto_enabled=False
        )
        == first
    )
    assert repository.owner_snapshot(actor).policy_revision == second.policy_revision
    assert repository.owner_snapshot(actor).auto_enabled


def test_execution_audit_requires_turn_but_memory_receipts_do_not():
    """Nullable audit Turn identity applies only to user-level memory responsibilities."""
    arguments = dict(
        tenant_id="t",
        subject_id="s",
        resource_id="m",
        resource_version="1",
        action="a",
        decision="d",
        policy_version="1",
        payload_hash="0" * 64,
    )
    assert AuditRecord(event_type=AuditEventType.MEMORY_COMMITTED, **arguments).turn_id is None
    with pytest.raises(ValidationError, match="real turn_id"):
        AuditRecord(event_type=AuditEventType.TOOL_ALLOWED, **arguments)


def test_new_unrelated_evidence_cannot_launder_an_old_profile_value(domain):
    """S12: evidence supporting another value cannot lend its source order to an old claim."""
    _, _, _, service, repository = domain
    actor, old_source, _ = add_user_source(domain, "以后用英文回答", "first")
    actor, new_source, _ = add_user_source(domain, "以后用中文回答", "second")
    tool_actor = actor.model_copy(update={"kind": "tool"})
    current = service.apply(
        tool_actor,
        language("new").model_copy(
            update={"evidence": (new_source.evidence_ref(),), "explicit_intent": False}
        ),
    )
    with pytest.raises(MemoryConflict, match="older evidence"):
        service.apply(
            tool_actor,
            language(
                "launder",
                "en",
                operation="update",
                memory_id=current.memory_id,
                expected_revision=current.revision,
            ).model_copy(
                update={
                    "evidence": (old_source.evidence_ref(), new_source.evidence_ref()),
                    "explicit_intent": False,
                }
            ),
        )
    assert repository.get(actor, current.memory_id).content == "zh-CN"


def test_two_create_candidates_cannot_both_reuse_an_absent_target_baseline(domain):
    """Creating an initially absent profile is also a versioned candidate precondition."""
    _, _, actor, service, repository = domain
    first = service.apply(
        actor,
        MemoryMutation(
            mutation_id="first-proposal",
            kind="profile",
            field="risk_statement",
            content="谨慎",
            explicit_intent=True,
        ),
    )
    second = service.apply(
        actor,
        MemoryMutation(
            mutation_id="second-proposal",
            kind="profile",
            field="risk_statement",
            content="谨慎",
            explicit_intent=True,
        ),
    )
    first_candidate = repository.get(actor, first.candidate_id, include_candidates=True)
    second_candidate = repository.get(actor, second.candidate_id, include_candidates=True)
    service.decide(
        actor,
        first_candidate.memory_id,
        "approve",
        "first-approve",
        1,
        first_candidate.content_hash,
    )
    with pytest.raises(MemoryConflict, match="target revision"):
        service.decide(
            actor,
            second_candidate.memory_id,
            "approve",
            "second-approve",
            1,
            second_candidate.content_hash,
        )
