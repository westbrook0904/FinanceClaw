"""Kill a real worker process during model I/O and recover its persistent attempt budget."""

import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.stage11.test_worker_pipeline import pipeline as pipeline_fixture

pipeline = pipeline_fixture

CHILD = r"""
import asyncio
import json
import sys
from pathlib import Path
from financeclaw.kernel.models import ModelProfile
from financeclaw.memory_worker.extraction import ExtractionHandler
from financeclaw.memory_worker.model import OfflineMemoryModel, StructuredMemoryModel
from financeclaw.memory_worker.runner import MemoryJobRunner
from financeclaw.shared.infrastructure.database import ApplicationDatabase
from financeclaw.shared.outbox.repository import SqlAlchemyOutboxRepository

class SlowModel(OfflineMemoryModel):
    async def ainvoke(self, messages):
        Path(sys.argv[3]).write_text("attempt_reserved")
        await asyncio.sleep(30)
        return await super().ainvoke(messages)

database = ApplicationDatabase(sys.argv[1])
outbox = SqlAlchemyOutboxRepository(database.session_factory)
profile = ModelProfile.model_validate(json.loads(sys.argv[2]))
model = StructuredMemoryModel(SlowModel() if sys.argv[3] else OfflineMemoryModel(), profile, outbox)
handler = ExtractionHandler(database.session_factory, outbox, model,
    consolidation_model_version=model.fingerprint)
runner = MemoryJobRunner(
    outbox, handler, destination="memory_extract", lease_seconds=1, renew_seconds=0.1)
try:
    asyncio.run(runner.run_once())
finally:
    database.close()
"""


@pytest.mark.asyncio
async def test_killed_worker_preserves_model_attempt_then_recovers(pipeline, tmp_path):
    """S17/S19: the replacement process receives a new lease and spends only remaining budget."""
    database, outbox, model, _, handler, _, _ = pipeline
    prepare = outbox.claim_pending(destination="memory_extract", limit=1)[0]
    await handler.process(prepare)
    marker = tmp_path / "model-started"
    executable = Path(__file__).resolve().parents[2] / ".venv/bin/python"
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("LANGSMITH_")
    }
    environment["LANGSMITH_TRACING"] = "false"
    arguments = [
        str(executable),
        "-c",
        CHILD,
        database.engine.url.render_as_string(hide_password=False),
        model.profile.model_dump_json(),
    ]
    child = subprocess.Popen(
        [*arguments, str(marker)],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                raise AssertionError(child.stderr.read())
            time.sleep(0.02)
        assert marker.exists(), "worker did not reach its durably reserved model request"
        child.kill()
        child.wait(timeout=5)
        time.sleep(1.15)
        resumed = subprocess.run(
            [*arguments, ""], env=environment, capture_output=True, text=True, timeout=15
        )
        assert resumed.returncode == 0, resumed.stderr
        from sqlalchemy import select

        from financeclaw.shared.outbox.tables import OutboxEventRow

        with database.session_factory() as session:
            part = session.scalar(
                select(OutboxEventRow).where(OutboxEventRow.event_type == "memory.extract.part")
            )
            assert part.status == "published"
            assert part.claim_epoch == 2
            assert part.processing_metadata["model_budget"]["attempts"] == 2
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
