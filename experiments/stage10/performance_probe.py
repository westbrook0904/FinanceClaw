"""Measure real product admission, native receipt, Journal and SSE on isolated PostgreSQL."""

import argparse
import asyncio
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg

REPORT = Path("/project/.redesign/evidence/stage10/performance.json")


def percentiles(values):
    """Use nearest rank without interpolating away tail samples."""
    ordered = sorted(values)
    return {
        f"p{p}": round(ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)], 2)
        for p in (50, 95, 99)
    }


async def batch(client, concurrency, *, subscribers=1):
    """Submit independent Turns and observe each through product SSE."""

    async def create():
        """Create the empty conversation outside measured admission latency."""
        response = await client.post("/v1/conversations", json={})
        response.raise_for_status()
        return response.json()["conversation_id"]

    conversations = await asyncio.gather(*(create() for _ in range(concurrency)))

    async def observe(path):
        """Timestamp the first committed terminal snapshot delivered on this connection."""
        async with client.stream("GET", path + "/events") as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = json.loads(line[6:])
                if payload.get("status") in {"completed", "failed", "cancelled"}:
                    assert payload["status"] == "completed", payload
                    return datetime.now(UTC)
        raise AssertionError("SSE closed without a terminal snapshot")

    async def task(conversation):
        """Measure admission separately from graph queueing and final delivery."""
        started = time.perf_counter()
        response = await client.post(
            f"/v1/conversations/{conversation}/turns",
            json={"message": '/tool calculate {"operation":"add","left":2,"right":3}'},
            headers={"Idempotency-Key": str(uuid4())},
        )
        elapsed = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        turn = response.json()["turn_id"]
        path = f"/v1/conversations/{conversation}/turns/{turn}"
        arrivals = await asyncio.gather(*(observe(path) for _ in range(subscribers)))
        return {
            "turn_id": turn,
            "admission_ms": elapsed,
            "sse_at": min(arrivals),
            "last_subscriber_at": max(arrivals),
        }

    started = time.perf_counter()
    rows = await asyncio.gather(*(task(conversation) for conversation in conversations))
    elapsed = time.perf_counter() - started
    app_dsn = os.environ["FINANCECLAW_DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(app_dsn) as app, psycopg.connect(os.environ["POSTGRES_URI"]) as native:
        for row in rows:
            with app.cursor() as cursor:
                cursor.execute(
                    """SELECT t.created_at, t.finished_at, c.receipt_bound_at,
                                      c.native_run_id, t.status
                    FROM conversation_turns t JOIN turn_commands c
                      ON c.command_id = t.current_command_id WHERE t.turn_id = %s""",
                    (row["turn_id"],),
                )
                created, finished, bound, native_id, status = cursor.fetchone()
                assert status == "completed" and bound and finished
            with native.cursor() as cursor:
                cursor.execute(
                    "SELECT updated_at, status FROM public.run WHERE run_id = %s", (native_id,)
                )
                native_finished, native_status = cursor.fetchone()
                assert native_status == "success"
            row.update(
                accepted_to_native_bound_ms=(bound - created).total_seconds() * 1000,
                native_terminal_to_journal_ms=(finished - native_finished).total_seconds() * 1000,
                journal_to_sse_ms=(row.pop("sse_at") - finished).total_seconds() * 1000,
                journal_to_last_subscriber_ms=(
                    row.pop("last_subscriber_at") - finished
                ).total_seconds()
                * 1000,
            )
    metrics = {
        name: percentiles([row[name] for row in rows]) for name in rows[0] if name.endswith("_ms")
    }
    return {
        "concurrency": concurrency,
        "subscribers_per_turn": subscribers,
        "duration_seconds": round(elapsed, 2),
        "metrics": metrics,
        "samples": rows,
    }


async def main(mode):
    """Record measured results, including misses, without converting targets into assertions."""
    async with httpx.AsyncClient(
        base_url="http://localhost:8000",
        timeout=180,
        limits=httpx.Limits(max_connections=512, max_keepalive_connections=256),
        headers={"Authorization": "Bearer stage10-product-test"},
    ) as client:
        await batch(client, 1)  # Exclude first graph compilation from the warm capacity samples.
        batches = []
        cases = ((1, 1), (32, 1), (128, 1), (1, 32)) if mode == "capacity" else ((32, 1),)
        for concurrency, subscribers in cases:
            result = await batch(client, concurrency, subscribers=subscribers)
            batches.append(result)
            print(
                json.dumps({key: value for key, value in result.items() if key != "samples"}),
                flush=True,
            )
    report = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    report[mode] = {
        "measured_at": datetime.now(UTC).isoformat(),
        "workload": "OfflineFinanceModel, synthetic calculate(2+3); no external provider calls",
        "api_jobs": 0,
        "worker_jobs": 4,
        "native_terminal_timestamp": "read-only public.run.updated_at after success; probe DB only",
        "batches": batches,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("capacity", "join_saturated"))
    asyncio.run(main(parser.parse_args().mode))
