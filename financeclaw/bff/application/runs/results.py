"""BFF result transactions are the sole completion writer for new root Turns."""

from datetime import datetime

from financeclaw.bff.application.runs.backend import interrupts
from financeclaw.bff.application.runs.interactions import public_interaction
from financeclaw.bff.application.runs.waits import map_wait
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.interactions import export
from financeclaw.shared.execution_ledger.repository import ExecutionConflict, digest
from financeclaw.shared.execution_ledger.root_repository import aware, now
from financeclaw.shared.execution_ledger.tables import RunExecutionRow


class ResultService:
    """Write exact observation, Journal, Turn, progress, audit and notification facts atomically."""

    def __init__(self, service):
        """Inject persistence and release declarations without a command backend."""
        self.service, self.store = service, service.store
        self.interactions = service.interactions.repository

    def blocked(self, claim, reason):
        """Keep a recoverable root visible without disclosing exception text or native state."""
        with self.store.sessions.begin() as session:
            row = self.store.lock(session, claim["run_id"], claim)
            root = session.get(RunExecutionRow, row.run_id)
            if row.active:
                self.store.project(
                    session,
                    row,
                    status="cancellation_requested"
                    if root.cancellation_requested
                    else "interrupted",
                    waiting_reason=reason,
                )

    def apply(self, claim, operation, observation):
        """Roll back all facts for stale ownership, cancellation or a failed Journal write."""
        with self.store.sessions.begin() as session:
            row = self.store.lock(session, claim["run_id"], claim)
            root = session.get(RunExecutionRow, row.run_id)
            if not row.active or root.cancellation_requested:
                return
            if root.server_run_id != operation["operation_id"]:
                raise ExecutionConflict("observation is no longer for the active BFF attempt")
            status = observation["status"]
            if status == "running":
                self.store.journal.update_turn_status(row.run_id, "running", session=session)
                self.store.project(
                    session, row, status="running", waiting_reason=None, pending_interactions=[]
                )
                return
            if status == "waiting":
                state = observation["state"]
                native = interrupts(state)[0]
                identifier = "interaction-" + digest(
                    [row.run_id, operation["operation_id"], native["id"]]
                )
                previous = session.get(PendingInteractionRow, identifier)
                if previous:
                    request = previous.request["bff"]
                    binding = request["binding"]
                    if (
                        binding["interrupt_hash"] != digest(native["value"])
                        or binding["checkpoint"] != state["checkpoint"]
                        or binding["native_run_id"] != observation["native_run_id"]
                    ):
                        raise ExecutionConflict("native wait changed without a new instance")
                    if previous.response is not None:
                        return
                else:
                    request = map_wait(
                        observation, root.snapshot, self.service.releases, self.service.settings
                    )
                saved = self.interactions.register(
                    row.run_id,
                    source="bff_" + request["source"],
                    server_run_id=operation["operation_id"],
                    interrupt_id=native["id"],
                    checkpoint_id=state["checkpoint"]["checkpoint_id"],
                    point_id=request["point"]["point_id"],
                    kind=request["point"]["kind"],
                    question=request["question"],
                    request={"bff": request},
                    expires_at=datetime.fromisoformat(request["expires_at"]),
                    now=now(),
                    identifier=identifier,
                    session=session,
                )
                root.waiting = {
                    "kind": "interaction",
                    "interaction_id": identifier,
                    "server_run_id": operation["operation_id"],
                }
                fact = {
                    "status": "waiting",
                    "interaction_id": identifier,
                    "binding": request["binding"],
                }
                self.store.execution.observe_in_session(
                    session,
                    operation["operation_id"],
                    server_run_id=operation["operation_id"],
                    result=fact,
                )
                self.store.journal.update_turn_status(row.run_id, "interrupted", session=session)
                self.store.project(
                    session,
                    row,
                    status="interrupted",
                    waiting_reason="interaction_expired"
                    if saved["status"] == "expired"
                    else "user_interaction",
                    pending_interactions=[public_interaction(saved)],
                )
                return
            if status not in {"completed", "failed"}:
                raise ExecutionConflict("unsupported BFF terminal observation")
            self.store.execution.observe_in_session(
                session,
                operation["operation_id"],
                server_run_id=operation["operation_id"],
                result=observation,
            )
            if status == "completed":
                self.store.journal.append_assistant_message(
                    run_id=row.run_id, content=observation["content"], session=session
                )
            else:
                self.store.journal.update_turn_status(row.run_id, "failed", session=session)
            root.waiting, row.active = None, False
            self.store.project(
                session,
                row,
                status=status,
                waiting_reason=observation.get("reason"),
                pending_interactions=[],
            )

    def expire(self, claim):
        """Periodic expiration is durable and independent of GET or SSE polling."""
        from sqlalchemy import select

        with self.store.sessions.begin() as session:
            row = self.store.lock(session, claim["run_id"], claim)
            for pending in session.scalars(
                select(PendingInteractionRow).where(
                    PendingInteractionRow.root_run_id == row.run_id,
                    PendingInteractionRow.status == "pending",
                )
            ):
                if aware(pending.expires_at) <= now():
                    self.interactions._close(session, pending, "expired", now())
                    self.store.project(
                        session,
                        row,
                        status="interrupted",
                        waiting_reason="interaction_expired",
                        pending_interactions=[public_interaction(export(pending))],
                    )

    def confirm_cancel(self, claim):
        """Commit cancelled only after the command service proves every possible attempt stopped."""
        with self.store.sessions.begin() as session:
            row = self.store.lock(session, claim["run_id"], claim)
            root = session.get(RunExecutionRow, row.run_id)
            if not row.active or not root.cancellation_requested:
                return
            root.cancellation_confirmed, root.waiting, row.active = True, None, False
            self.store.journal.confirm_cancel(row.run_id, session=session)
            self.store.project(
                session, row, status="cancelled", waiting_reason=None, pending_interactions=[]
            )
