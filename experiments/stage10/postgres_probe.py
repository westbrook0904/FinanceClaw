"""Concurrency and constraints against a freshly migrated disposable PostgreSQL database."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from financeclaw.api.application.turns.bootstrap import build_turns
from financeclaw.api.application.turns.commands import CommandService
from financeclaw.kernel.responses import ConversationTurnRequest
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from financeclaw.shared.turns.tables import ConversationTurnRow, TurnCommandRow
from financeclaw.shared.turns.types import ExecutionConflict, now


def run():
    """Use only the explicit probe database; never initialize or reset a developer database."""
    settings = FinanceClawSettings()
    assert settings.database_url.get_secret_value().endswith("/stage10_migration")
    resources = build_resources(settings, enable_persistence=True)
    service = build_turns(settings, resources, client=object())
    try:
        tables = set(inspect(resources.database.engine).get_table_names())
        assert len(tables - {"alembic_version"}) == 14
        conversation = service.journal.create_conversation(
            tenant_id="race",
            subject_id="race",
            agent_id="finance_agent",
            agent_profile_version="1.6.0",
        )
        key = str(uuid4())

        def admit(_):
            """Race the same accepted request through independent database sessions."""
            return service.admission.accept(
                conversation.conversation_id,
                ConversationTurnRequest(message="synthetic PostgreSQL race"),
                tenant_id="race",
                subject_id="race",
                scopes={"*"},
                idempotency_key=key,
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            accepted = list(pool.map(admit, range(32)))
        assert len({item.turn_id for item in accepted}) == 1
        turn_id = accepted[0].turn_id

        def claim(index):
            """Competing API replicas can acquire only one current lease."""
            return service.store.claim_due("replica-" + str(index))

        with ThreadPoolExecutor(max_workers=16) as pool:
            leases = [value for value in pool.map(claim, range(32)) if value]
        assert len(leases) == 1
        command_service = CommandService(service, None)
        turn, command = command_service.read(leases[0])
        assert turn["turn_id"] == turn_id
        command_service.claim_send(leases[0], command["command_id"])
        command_service.bind(leases[0], command["command_id"], str(uuid4()))
        with service.sessions.begin() as session:
            row = session.get(ConversationTurnRow, turn_id)
            row.release_snapshot = {
                **row.release_snapshot,
                "limits": {**row.release_snapshot["limits"], "model": 5},
            }

        def budget(_):
            """Count actual successful atomic reservations at the shared Turn budget."""
            try:
                service.execution.consume(turn_id, "model")
                return 1
            except ExecutionConflict:
                return 0

        with ThreadPoolExecutor(max_workers=16) as pool:
            assert sum(pool.map(budget, range(64))) == 5
        with service.sessions() as session:
            counts = session.scalar(
                select(ConversationTurnRow.model_calls).where(
                    ConversationTurnRow.turn_id == turn_id
                )
            )
        try:
            with service.sessions.begin() as session:
                row = session.get(ConversationTurnRow, turn_id)
                row.current_command_id = str(uuid4())
        except IntegrityError:
            pass
        else:
            raise AssertionError("missing deferred current-command foreign key")
        with service.sessions.begin() as session:
            row = session.get(ConversationTurnRow, turn_id)
            row.status, row.finished_at = "cancelled", now()
            session.get(TurnCommandRow, row.current_command_id).state = "observed"
        report = {
            "passed": True,
            "application_tables": sorted(tables - {"alembic_version"}),
            "same_key_requests": 32,
            "accepted_turns": 1,
            "replica_claims": 32,
            "lease_winners": 1,
            "budget_attempts": 64,
            "model_calls": counts,
            "deferred_command_fk": True,
        }
        path = Path("/project/.redesign/evidence/stage10/postgres-contract.json")
        path.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    finally:
        resources.database.close()


if __name__ == "__main__":
    run()
