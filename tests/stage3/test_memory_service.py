"""`test_memory_service` 模块提供`stage3`相关能力。"""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.store.memory import InMemoryStore
from pydantic import ValidationError

from financeclaw.kernel import ExecutionContext
from financeclaw.modules.audit import AuditEventType, InMemoryAuditRepository
from financeclaw.modules.memory import (
    LongTermMemoryService,
    MemoryConfirmationRequired,
    MemoryDraft,
    MemoryPolicy,
    MemoryPolicyViolation,
    MemoryStatus,
)

from .support import conversation_context, journal


def test_draft_shape_and_financial_fact_boundary() -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(ValidationError):
        MemoryDraft.model_validate(
            {
                "kind": "preference",
                "content": "偏好低波动资产",
                "evidence_message_ids": ["current"],
                "tenant_id": "model-forged-tenant",
            }
        )
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(ValidationError):
        MemoryDraft(
            kind="domain_fact",
            content="AAPL is attractive",
            evidence_message_ids=("current",),
        )
    # 准备 policy，供后续步骤使用。
    policy = MemoryPolicy()
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(MemoryPolicyViolation, match="financial facts"):
        policy.assess(
            MemoryDraft(
                kind="decision_note",
                content="AAPL current price is 250 USD",
                evidence_message_ids=("current",),
            )
        )
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(MemoryPolicyViolation, match="credentials"):
        policy.assess(
            MemoryDraft(
                kind="constraint",
                content="API key: sk-abcdefghijklmnop",
                evidence_message_ids=("current",),
            )
        )


def test_trusted_namespace_evidence_lifecycle_and_cross_thread_recall(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database and repository，供后续步骤使用。
    database, repository = journal(tmp_path / "memory.db")
    # 准备 audit，供后续步骤使用。
    audit = InMemoryAuditRepository()
    # 准备 service，供后续步骤使用。
    service = LongTermMemoryService(
        conversation_repository=repository,
        audit=audit,
    )
    # 准备 store，供后续步骤使用。
    store = InMemoryStore()
    # 准备 first_context and first_message_id，供后续步骤使用。
    first_context, first_message_id = conversation_context(repository)
    # 准备 draft，供后续步骤使用。
    draft = MemoryDraft(
        kind="preference",
        content="用户偏好低波动资产",
        evidence_message_ids=("current",),
    )
    # 准备 proposal，供后续步骤使用。
    proposal = service.propose(first_context, draft)
    # 继续执行前验证内部不变量。
    assert proposal.draft.evidence_message_ids == (first_message_id,)
    # 继续执行前验证内部不变量。
    assert first_context.tenant_id not in service.namespace(first_context)
    # 继续执行前验证内部不变量。
    assert first_context.subject_id not in service.namespace(first_context)

    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(MemoryConfirmationRequired):
        service.confirm(
            first_context,
            store,
            proposal_id=proposal.proposal_id,
            draft=proposal.draft,
            user_confirmed=False,
        )
    # 准备 first，供后续步骤使用。
    first = service.confirm(
        first_context,
        store,
        proposal_id=proposal.proposal_id,
        draft=proposal.draft,
        user_confirmed=True,
    )
    # 继续执行前验证内部不变量。
    assert first.status is MemoryStatus.ACTIVE
    # 继续执行前验证内部不变量。
    assert first.namespace == service.namespace(first_context)

    # 同一认证主体创建新会话时仍应解析到相同的 Store 命名空间，伪造的租户或主体不得访问该空间。
    second_context, second_message_id = conversation_context(
        repository,
        message="把风险偏好改成只投资现金类资产",
        key="replacement-turn",
    )
    # 准备 recalls，供后续步骤使用。
    recalls = service.search(
        second_context,
        store,
        query="低波动",
        for_model_context=True,
    )
    # 继续执行前验证内部不变量。
    assert tuple(item.record.memory_id for item in recalls) == (first.memory_id,)
    # 准备 other_subject，供后续步骤使用。
    other_subject = second_context.model_copy(
        update={"subject_id": "subject.b", "conversation_id": None}
    )
    # 继续执行前验证内部不变量。
    assert service.search(other_subject, store, query="低波动") == ()

    # 准备 replacement_proposal，供后续步骤使用。
    replacement_proposal = service.propose(
        second_context,
        MemoryDraft(
            kind="preference",
            content="用户只投资现金类资产",
            evidence_message_ids=(second_message_id,),
        ),
    )
    # 准备 replacement，供后续步骤使用。
    replacement = service.confirm(
        second_context,
        store,
        proposal_id=replacement_proposal.proposal_id,
        draft=replacement_proposal.draft,
        user_confirmed=True,
        supersedes_id=first.memory_id,
    )
    # 继续执行前验证内部不变量。
    assert replacement.supersedes_id == first.memory_id
    # 继续执行前验证内部不变量。
    assert service.get(second_context, store, first.memory_id) is None
    # 继续执行前验证内部不变量。
    assert (
        service.get(second_context, store, first.memory_id, include_inactive=True).status
        is MemoryStatus.SUPERSEDED
    )

    # 准备 revoked，供后续步骤使用。
    revoked = service.forget(second_context, store, replacement.memory_id, mode="revoke")
    # 继续执行前验证内部不变量。
    assert revoked.status is MemoryStatus.REVOKED
    # 继续执行前验证内部不变量。
    assert service.search(second_context, store, query=None) == ()
    # 准备 deleted，供后续步骤使用。
    deleted = service.forget(second_context, store, replacement.memory_id, mode="delete")
    # 继续执行前验证内部不变量。
    assert deleted.status is MemoryStatus.DELETED
    # 继续执行前验证内部不变量。
    assert service.get(second_context, store, replacement.memory_id) is None

    # 准备 memory_events，供后续步骤使用。
    memory_events = [
        record.event_type for record in audit.records() if record.resource_type == "memory"
    ]
    # 继续执行前验证内部不变量。
    assert memory_events == [
        AuditEventType.MEMORY_PROPOSED,
        AuditEventType.MEMORY_COMMITTED,
        AuditEventType.MEMORY_PROPOSED,
        AuditEventType.MEMORY_COMMITTED,
        AuditEventType.MEMORY_SUPERSEDED,
        AuditEventType.MEMORY_REVOKED,
        AuditEventType.MEMORY_DELETED,
    ]
    # 前置条件满足后调用 close。
    database.close()


def test_evidence_cannot_cross_conversation_or_owner(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database and repository，供后续步骤使用。
    database, repository = journal(tmp_path / "evidence.db")
    # 准备 context_a and message_a，供后续步骤使用。
    context_a, message_a = conversation_context(repository, key="owner-a")
    # 准备 context_b and _，供后续步骤使用。
    context_b, _ = conversation_context(
        repository,
        subject_id="subject.b",
        message="另一个用户的偏好",
        key="owner-b",
    )
    # 准备 service，供后续步骤使用。
    service = LongTermMemoryService(
        conversation_repository=repository,
        audit=InMemoryAuditRepository(),
    )
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(RuntimeError, match="evidence"):
        service.propose(
            context_b,
            MemoryDraft(
                kind="preference",
                content="试图引用另一个用户",
                evidence_message_ids=(message_a,),
            ),
        )
    # 准备 forged_context，供后续步骤使用。
    forged_context = ExecutionContext(
        tenant_id="tenant.b",
        subject_id=context_a.subject_id,
        conversation_id=context_a.conversation_id,
        turn_id=context_a.turn_id,
        run_id=context_a.run_id,
    )
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(LookupError):
        service.propose(
            forged_context,
            MemoryDraft(
                kind="preference",
                content="试图伪造租户",
                evidence_message_ids=(message_a,),
            ),
        )
    # 前置条件满足后调用 close。
    database.close()


def test_store_value_cannot_forge_identity_inside_trusted_namespace(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 database and repository，供后续步骤使用。
    database, repository = journal(tmp_path / "corrupt-store.db")
    # 准备 context and _，供后续步骤使用。
    context, _ = conversation_context(repository, key="corrupt-record")
    # 准备 service，供后续步骤使用。
    service = LongTermMemoryService(
        conversation_repository=repository,
        audit=InMemoryAuditRepository(),
    )
    # 准备 store，供后续步骤使用。
    store = InMemoryStore()
    # 准备 proposal，供后续步骤使用。
    proposal = service.propose(
        context,
        MemoryDraft(
            kind="constraint",
            content="用户不使用杠杆",
            evidence_message_ids=("current",),
        ),
    )
    # 准备 record，供后续步骤使用。
    record = service.confirm(
        context,
        store,
        proposal_id=proposal.proposal_id,
        draft=proposal.draft,
        user_confirmed=True,
    )
    # 准备 forged，供后续步骤使用。
    forged = record.model_copy(update={"tenant_id": "tenant.b"})
    # 前置条件满足后调用 put。
    store.put(
        service.namespace(context),
        forged.memory_id,
        forged.model_dump(mode="json"),
        index=False,
    )
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with pytest.raises(RuntimeError, match="identity"):
        service.search(context, store)
    # 前置条件满足后调用 close。
    database.close()


@pytest.mark.external
@pytest.mark.skipif(
    not os.getenv("FINANCECLAW_SPIKE_POSTGRES_DSN"),
    reason="PostgreSQL Store probe setting is not configured",
)
def test_postgres_store_record_survives_connection_reconstruction(tmp_path: Path) -> None:
    """验证函数名所描述的业务场景符合预期。"""
    from langgraph.store.postgres import PostgresStore

    # 准备 database and repository，供后续步骤使用。
    database, repository = journal(tmp_path / "postgres-store.db")
    # 准备 unique，供后续步骤使用。
    unique = uuid4().hex
    # 准备 context and _，供后续步骤使用。
    context, _ = conversation_context(
        repository,
        tenant_id=f"stage3-{unique}",
        subject_id=f"stage3-{unique}",
        key=f"postgres-{unique}",
    )
    # 准备 service，供后续步骤使用。
    service = LongTermMemoryService(
        conversation_repository=repository,
        audit=InMemoryAuditRepository(),
    )
    # 准备 draft，供后续步骤使用。
    draft = MemoryDraft(
        kind="goal",
        content="用户希望建立长期低波动组合",
        evidence_message_ids=("current",),
    )
    # 准备 proposal，供后续步骤使用。
    proposal = service.propose(context, draft)
    # 准备 dsn，供后续步骤使用。
    dsn = os.environ["FINANCECLAW_SPIKE_POSTGRES_DSN"]
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with PostgresStore.from_conn_string(dsn) as first_store:
        first_store.setup()
        committed = service.confirm(
            context,
            first_store,
            proposal_id=proposal.proposal_id,
            draft=proposal.draft,
            user_confirmed=True,
        )
    # 限定依赖资源的生命周期，并确保资源能够可靠释放。
    with PostgresStore.from_conn_string(dsn) as restarted_store:
        restored = service.get(context, restarted_store, committed.memory_id)
        assert restored == committed
        # 测试仅操作自己创建的唯一命名空间，并在验证持久化后清理夹具，避免共享开发数据库持续增长。
        restarted_store.delete(service.namespace(context), committed.memory_id)
    # 前置条件满足后调用 close。
    database.close()
