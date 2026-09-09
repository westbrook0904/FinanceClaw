"""Inspect and configure BFF admission and background dispatch."""

import argparse
import json

from sqlalchemy import func, select, update

from financeclaw.bff.application.runs.store import BFFRunRepository
from financeclaw.shared.execution_ledger.control_tables import RunControlRow
from financeclaw.shared.execution_ledger.driver import control
from financeclaw.shared.execution_ledger.repository import ExecutionConflict
from financeclaw.shared.execution_ledger.root_repository import now
from financeclaw.shared.execution_ledger.run_tables import RootRunRow
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


class BFFDeploymentControl:
    """CAS on the shared control revision protects concurrent deployment decisions."""

    def __init__(self, store):
        """Use the initialized business database."""
        self.store = store

    def view(self):
        """Report controls and root counts without messages, identities, or backend calls."""
        self.store.require_schema()
        with self.store.sessions() as session:
            gate = session.get(RunControlRow, 1)
            counts = session.execute(
                select(RootRunRow.driver_version, RootRunRow.active, func.count()).group_by(
                    RootRunRow.driver_version, RootRunRow.active
                )
            )
            return {
                "revision": gate.revision,
                "bff_admission_enabled": gate.bff_admission_enabled,
                "bff_dispatch_paused": gate.bff_dispatch_paused,
                "roots": [
                    {"driver_version": driver, "active": active, "count": count}
                    for driver, active, count in counts
                ],
            }

    def configure(self, expected_revision, *, admission_enabled=None, dispatch_paused=None):
        """Change admission and dispatch under an exclusive control lock and revision CAS."""
        self.store.require_schema()
        with self.store.sessions.begin() as session:
            control(session, exclusive=True)
            changed = session.execute(
                update(RunControlRow)
                .where(RunControlRow.control_id == 1, RunControlRow.revision == expected_revision)
                .values(revision=RunControlRow.revision + 1)
            ).rowcount
            if changed != 1:
                raise ExecutionConflict("control revision does not match")
            gate = session.get(RunControlRow, 1, populate_existing=True)
            if admission_enabled is not None:
                gate.bff_admission_enabled = admission_enabled
            if dispatch_paused is not None:
                gate.bff_dispatch_paused = dispatch_paused
            gate.updated_at = now()
        return self.view()


def main():
    """Inspect or explicitly configure BFF controls without starting a runtime or applying DDL."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    configure = sub.add_parser("configure")
    configure.add_argument("--expected-revision", type=int, required=True)
    configure.add_argument("--admission-enabled", choices=("true", "false"))
    configure.add_argument("--dispatch-paused", choices=("true", "false"))
    args = parser.parse_args()
    settings = FinanceClawSettings()
    database = ApplicationDatabase(settings.database_url.get_secret_value())
    try:
        control = BFFDeploymentControl(
            BFFRunRepository(
                database.session_factory, backend_instance_id=settings.bff_backend_instance_id
            )
        )
        if args.action == "status":
            result = control.view()
        else:
            result = control.configure(
                args.expected_revision,
                admission_enabled=None
                if args.admission_enabled is None
                else args.admission_enabled == "true",
                dispatch_paused=None
                if args.dispatch_paused is None
                else args.dispatch_paused == "true",
            )
        print(json.dumps(result))
    finally:
        database.close()


if __name__ == "__main__":
    main()
