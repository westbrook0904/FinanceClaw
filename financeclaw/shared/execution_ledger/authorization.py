"""协调端与执行端共用的任务授权复验，不把服务凭据当作用户授权。"""

from datetime import UTC, datetime

from financeclaw.shared.execution_ledger.coordination_tables import RunAuthorizationRow


def intersect_scopes(original, current) -> frozenset[str]:
    """通配范围也只能收窄；输出是固定操作实际获得的上界。"""
    original, current = frozenset(original), frozenset(current)
    return current if "*" in original else original if "*" in current else original & current


def require_scopes(granted, required) -> None:
    """明确拒绝缺失权限；空 required 不会产生授权。"""
    if "*" not in granted and not frozenset(required).issubset(granted):
        raise PermissionError("required execution scope is missing")


def check_authorization(session, root, *, scopes=None, now=None):
    """已协调根必须持有未撤销且有效的 grant，原身份和发布不受续期影响。"""
    from financeclaw.shared.execution_ledger.repository import ExecutionConflict

    if root is None:
        raise ExecutionConflict("root execution is unavailable")
    if root.snapshot.get("driver_mode") != "coordinator":
        return None
    grant = session.get(RunAuthorizationRow, root.run_id)
    expires = (
        grant.expires_at.replace(tzinfo=UTC)
        if grant and grant.expires_at.tzinfo is None
        else grant.expires_at
        if grant
        else None
    )
    if grant is None or grant.revoked or expires <= (now or datetime.now(UTC)):
        raise ExecutionConflict("task authorization expired or revoked")
    if scopes is not None and "*" not in grant.scopes and not set(scopes).issubset(grant.scopes):
        raise ExecutionConflict("execution scopes exceed current task authorization")
    return grant
