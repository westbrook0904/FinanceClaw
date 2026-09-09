"""BFF 根事实与运行命令的事务入口。"""

from datetime import datetime

from financeclaw.shared.execution_ledger.authorization import check_authorization
from financeclaw.shared.execution_ledger.driver import control
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.execution_ledger.root_repository import RootRunRepository, now
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow

DRIVER_VERSION = 1


class BFFRunRepository(RootRunRepository):
    """Only the current BFF protocol can claim or mutate a root."""

    driver_version = DRIVER_VERSION

    def claim_operation(self, claim, operation_id):
        """Commit a single sending right after checking the frozen command and live grant."""
        with self.sessions.begin() as session:
            if control(session).bff_dispatch_paused:
                return False
            row = self.lock(session, claim["run_id"], claim)
            root = session.get(RunExecutionRow, row.run_id)
            operation = session.get(RunOperationRow, operation_id)
            if (
                operation is None
                or operation.run_id != root.run_id
                or not row.active
                or root.cancellation_requested
            ):
                raise ExecutionConflict("root does not permit BFF dispatch")
            request = operation.request
            check_authorization(session, root, scopes=request["scopes"])
            if request["kind"] == "resume":
                if root.server_run_id != request["predecessor"]:
                    raise ExecutionConflict("resume predecessor changed")
                if datetime.fromisoformat(request["payload"]["expires_at"]) <= now():
                    raise ExecutionConflict("accepted interaction expired before dispatch")
            elif request["kind"] != "start":
                raise ExecutionConflict("BFF accepts only root start and human resume commands")
            return self.execution.claim(operation_id, session=session)
