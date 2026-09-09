"""BFF 受理和调度使用的事务门闩。"""

from sqlalchemy import select

from financeclaw.shared.execution_ledger.control_tables import RunControlRow
from financeclaw.shared.execution_ledger.repository import ExecutionConflict


def control(session, *, exclusive=False):
    """首先持有共享门闩锁，随后才能取得会话／根锁；暂停使用独占锁。"""
    row = session.scalar(
        select(RunControlRow)
        .where(RunControlRow.control_id == 1)
        .with_for_update(read=not exclusive)
    )
    if row is None:
        raise ExecutionConflict("BFF run control is missing")
    return row
