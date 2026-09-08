"""数据库内的驱动隔离；部署停止证明负责覆盖不理解门闩的旧二进制。"""

from sqlalchemy import select

from financeclaw.shared.execution_ledger.cutover_tables import CoordinationControlRow
from financeclaw.shared.execution_ledger.repository import ExecutionConflict


def control(session):
    """首先持有共享门闩锁，随后才能取得会话／根锁；暂停使用独占锁。"""
    row = session.scalar(
        select(CoordinationControlRow)
        .where(CoordinationControlRow.control_id == 1)
        .with_for_update(read=True)
    )
    if row is None:
        raise ExecutionConflict("coordination deployment control is missing")
    return row


def require_legacy(session, snapshot):
    """同一领取事务再次验证归属，阻断旧服务检查后跨越接管边界的提交。"""
    gate = control(session)
    if snapshot.get("driver_mode") == "coordinator":
        raise ExecutionConflict(
            "task is exclusively managed by Coordinator; legacy dispatch is fenced"
        )
    if gate.legacy_fenced or snapshot.get("driver_mode", "legacy") != "legacy":
        raise ExecutionConflict("legacy dispatch is fenced")
