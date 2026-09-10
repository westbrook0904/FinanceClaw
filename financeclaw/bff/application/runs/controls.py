"""停止与有限授权的事务核心，由 API、命令和卡片共同使用。"""

from financeclaw.shared.execution_ledger.authorization import intersect_scopes
from financeclaw.shared.execution_ledger.repository import ExecutionConflict, snapshot_context
from financeclaw.shared.execution_ledger.run_tables import RunAuthorizationRow
from financeclaw.shared.execution_ledger.tables import RunExecutionRow


def apply_control(service, session, root, op, *, scopes=(), authorization=None, revision=None):
    """在调用者持有的根锁内修改决定与唤醒，绝不等待后端网络。"""
    from financeclaw.bff.application.runs.service import bounded_authorization
    from financeclaw.shared.execution_ledger.root_repository import now

    execution = session.get(RunExecutionRow, root.run_id)
    grant = session.get(RunAuthorizationRow, root.run_id)
    if op == "cancel":
        if root.active and not execution.cancellation_requested:
            service.interactions.repository.cancel_root(root.run_id, now=now(), session=session)
            service.repository.update_turn_status(
                root.run_id, "cancellation_requested", session=session
            )
            service.store.command_inbox(session, root, "cancel")
            service.store.project(
                session,
                root,
                status="cancellation_requested",
                waiting_reason="execution_stop_not_confirmed",
                pending_interactions=[],
            )
        return
    if op not in {"authorize", "revoke"}:
        raise ExecutionConflict("unsupported task control")
    if revision is not None and grant.revision != revision:
        raise ExecutionConflict("authorization view is stale")
    if not root.active:
        raise ExecutionConflict("task has already ended")
    if op == "authorize":
        if execution.cancellation_requested:
            raise ExecutionConflict("cancelling task cannot be reauthorized")
        context = snapshot_context(execution.snapshot)
        evidence, expires = bounded_authorization(
            service.settings,
            tenant_id=context.tenant_id,
            subject_id=context.subject_id,
            scopes=scopes,
            evidence=authorization,
        )
        grant.scopes = sorted(intersect_scopes(context.scopes, scopes))
        grant.source, grant.source_hash, grant.issued_at = (
            evidence.source,
            evidence.source_hash,
            evidence.issued_at,
        )
        grant.expires_at, grant.revoked = expires, False
    elif grant.revoked:
        return
    else:
        grant.revoked = True
    grant.revision += 1
    service.store.authorization_event(
        session, root, grant, "reauthorized" if op == "authorize" else "revoked"
    )
    service.store.command_inbox(session, root, f"{op}:{grant.revision}")
    service.store.project(
        session,
        root,
        authorization_revision=grant.revision,
        status="cancellation_requested" if execution.cancellation_requested else "interrupted",
        waiting_reason="execution_stop_not_confirmed"
        if execution.cancellation_requested
        else "authorization_updated"
        if op == "authorize"
        else "authorization_required",
    )
