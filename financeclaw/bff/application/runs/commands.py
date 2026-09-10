"""Recover only persisted BFF root commands; no graph-derived scheduling or child dispatch."""

import asyncio

from sqlalchemy import select

from financeclaw.kernel.backend import BackendExecutionRef
from financeclaw.shared.execution_ledger.authorization import check_authorization
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow


class RunCommandService:
    """Own the sending right, uncertain receipt recovery and exact root cancellation."""

    def __init__(self, store, backend, reader, results):
        """Keep network calls outside root transactions and receipt binding inside them."""
        self.store, self.backend, self.reader, self.results = store, backend, reader, results

    def read(self, claim):
        """Copy durable facts under the active lease before making remote calls."""
        with self.store.sessions.begin() as session:
            row = self.store.lock(session, claim["run_id"], claim)
            root = session.get(RunExecutionRow, row.run_id)
            operations = [
                {c.name: getattr(op, c.name) for c in op.__table__.columns}
                for op in session.scalars(
                    select(RunOperationRow)
                    .where(RunOperationRow.run_id == row.run_id)
                    .order_by(RunOperationRow.created_at)
                )
            ]
            attempts = {
                op["operation_id"]: BackendExecutionRef.model_validate(op["reference"])
                for op in operations
                if op["reference"] is not None
            }
            return {
                "active": row.active,
                "cancelled": root.cancellation_requested,
                "snapshot": root.snapshot,
                "current": root.server_run_id,
                "operations": operations,
                "attempts": attempts,
            }

    def uncertain(self, claim, operation_id):
        """Never turn a lost receipt or sending-process death back into prepared."""
        with self.store.sessions.begin() as session:
            self.store.lock(session, claim["run_id"], claim)
            self.store.execution.uncertain(operation_id, session=session)

    def authorized(self, claim):
        """Check authority only for new sends; observation/receipt lookup remain available."""
        with self.store.sessions.begin() as session:
            row = self.store.lock(session, claim["run_id"], claim)
            check_authorization(session, session.get(RunExecutionRow, row.run_id))

    async def recover(self, claim):
        """Handle bounded existing commands; return whether the active attempt can be observed."""
        state = await asyncio.to_thread(self.read, claim)
        if not state["active"]:
            return False
        unknown = next(
            (
                op
                for op in state["operations"]
                if op["status"] in {"claimed", "uncertain"}
                and op["operation_id"] not in state["attempts"]
            ),
            None,
        )
        if unknown:
            reference = await self.reader.lookup(unknown, state["snapshot"])
            if reference is None:
                await asyncio.to_thread(self.results.blocked, claim, "submission_uncertain")
                return False
            await asyncio.to_thread(self.store.bind, claim, reference)
            state = await asyncio.to_thread(self.read, claim)
        if state["cancelled"]:
            for reference in state["attempts"].values():
                if not await self.backend.cancel(reference):
                    return False
            await asyncio.to_thread(self.results.confirm_cancel, claim)
            return False
        prepared = [op for op in state["operations"] if op["status"] == "prepared"]
        if len(prepared) > 1:
            raise ExecutionConflict("root has multiple unsubmitted BFF commands")
        if prepared:
            operation = prepared[0]
            try:
                await asyncio.to_thread(self.authorized, claim)
            except ExecutionConflict:
                await asyncio.to_thread(self.results.blocked, claim, "authorization_required")
                return False
            if not await asyncio.to_thread(
                self.store.claim_operation, claim, operation["operation_id"]
            ):
                return False
            operation = await asyncio.to_thread(
                self.store.execution.operation, operation["operation_id"]
            )
            try:
                reference = await self.backend.submit(operation, state["snapshot"])
            except (Exception, asyncio.CancelledError):
                await asyncio.to_thread(self.uncertain, claim, operation["operation_id"])
                raise
            await asyncio.to_thread(self.store.bind, claim, reference)
        return True


class RunObserver:
    """Observe exact receipts and submit facts; this object has no command backend."""

    def __init__(self, store, reader, results):
        """Expose only the read capability to the observer."""
        self.store, self.reader, self.results = store, reader, results

    def current(self, claim):
        """Resolve the current operation by immutable receipt mapping under the root fence."""
        with self.store.sessions.begin() as session:
            row = self.store.lock(session, claim["run_id"], claim)
            root = session.get(RunExecutionRow, row.run_id)
            if not row.active or not root.server_run_id:
                return None
            op = session.get(RunOperationRow, root.server_run_id)
            return (
                {c.name: getattr(op, c.name) for c in op.__table__.columns},
                BackendExecutionRef.model_validate(op.reference),
                root.snapshot,
            )

    async def observe(self, claim):
        """Backend success/error hints never substitute for verified checkpoint evidence."""
        await asyncio.to_thread(self.results.expire, claim)
        current = await asyncio.to_thread(self.current, claim)
        if current:
            operation, reference, snapshot = current
            observation = await self.reader.observe(reference, operation, snapshot)
            await asyncio.to_thread(self.results.apply, claim, operation, observation)
