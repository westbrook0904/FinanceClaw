"""渠道和控制命令复用 Inbox 保存持久回执，避免额外的幂等业务表。"""

from datetime import timedelta

from financeclaw.shared.execution_ledger.repository import ExecutionConflict, digest
from financeclaw.shared.execution_ledger.root_repository import now
from financeclaw.shared.execution_ledger.run_tables import RunInboxRow


def read_receipt(session, root, key, fingerprint):
    """根锁下校验原键与原内容，只返回已经提交的结果。"""
    row = session.get(RunInboxRow, digest([root.run_id, "user_command", key]))
    if row is None:
        return None
    if row.payload["hash"] != fingerprint:
        raise ExecutionConflict("command key was used with different content")
    return row.payload["result"]


def save_receipt(session, root, key, fingerprint, result):
    """与业务决定同事务保存回执，协调器不再次处理用户命令。"""
    session.add(
        RunInboxRow(
            inbox_id=digest([root.run_id, "user_command", key]),
            kind="user_command",
            run_id=root.run_id,
            backend_instance_id=root.backend_instance_id,
            payload={"hash": fingerprint, "result": result},
            processed=True,
            expires_at=now() + timedelta(days=7),
        )
    )
