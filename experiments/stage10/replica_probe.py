"""Exercise cross-API admission and SSE, plus the real integrations history consumer."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
from langgraph_sdk import get_client

from financeclaw.shared.memory.namespace import history_namespace

REPORT = Path("/project/.redesign/evidence/stage10/replicas-integrations.json")


async def main():
    """Use two independent native API processes over the same durable application facts."""
    async with httpx.AsyncClient(
        timeout=90, headers={"Authorization": "Bearer stage10-product-test"}
    ) as client:
        first, second = "http://product-api:8000", "http://product-api-2:8000"
        for _ in range(120):
            try:
                if (await client.get(second + "/v1/health/ready")).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("second API is not ready")
        response = await client.post(first + "/v1/conversations", json={})
        response.raise_for_status()
        conversation = response.json()["conversation_id"]
        key = str(uuid4())
        path = f"/v1/conversations/{conversation}/turns"

        async def submit(index):
            """Race the same business intent on both API replicas."""
            response = await client.post(
                (first if index % 2 else second) + path,
                json={"message": '/tool calculate {"operation":"add","left":2,"right":3}'},
                headers={"Idempotency-Key": key},
            )
            response.raise_for_status()
            return response.json()

        results = await asyncio.gather(*(submit(index) for index in range(32)))
        identifiers = {value["turn_id"] for value in results}
        assert len(identifiers) == 1
        turn = identifiers.pop()
        async with client.stream("GET", second + path + "/" + turn + "/events") as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    snapshot = json.loads(line[6:])
                    if snapshot.get("status") == "completed":
                        break
                    assert snapshot.get("status") not in {"failed", "cancelled"}, snapshot
            else:
                raise AssertionError("cross-replica SSE ended without a final answer")
        dsn = os.environ["FINANCECLAW_DATABASE_URL"].replace("+psycopg", "")
        with psycopg.connect(dsn, autocommit=True) as connection:
            for _ in range(120):
                status = connection.execute(
                    "SELECT status FROM outbox_events WHERE event_id=%s", ("history-index:" + turn,)
                ).fetchone()
                if status and status[0] == "published":
                    break
                await asyncio.sleep(0.25)
            else:
                raise AssertionError("integrations did not publish this history intent")
            commands, receipts = connection.execute(
                "SELECT count(*),count(native_run_id) FROM turn_commands WHERE turn_id=%s", (turn,)
            ).fetchone()
            assert commands == receipts == 1
            messages = connection.execute(
                "SELECT count(*) FROM conversation_messages WHERE turn_id=%s", (turn,)
            ).fetchone()[0]
            assert messages == 2
        store = get_client(
            url=first,
            api_key=None,
            headers={"Authorization": "Bearer stage10-integration-service-test-token"},
        )
        try:
            namespace = history_namespace(
                SimpleNamespace(tenant_id="probe", subject_id="probe"), conversation
            )
            indexed = await store.store.search_items(namespace, filter={"turn_id": turn}, limit=100)
            assert len(indexed["items"]) == 2
        finally:
            await store.aclose()
        report = {
            "passed": True,
            "api_replicas": 2,
            "same_key_requests": 32,
            "turn_id": turn,
            "commands": commands,
            "native_receipts": receipts,
            "journal_messages": messages,
            "cross_replica_sse": True,
            "history_outbox_published": True,
            "native_store_indexed_items": 2,
        }
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(main())
