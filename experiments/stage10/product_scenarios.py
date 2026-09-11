"""Persisted product scenarios run in phases around actual API and Worker restarts."""

import argparse
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from langgraph_sdk import get_client

REPORT = Path("/project/.redesign/evidence/stage10/product-recovery.json")
OWNER_TOKEN = "stage10-product-test"
SERVICE_TOKEN = "stage10-integration-service-test-token"


def run(phase):
    """Use synthetic data only; phase boundaries are actual Compose process restarts."""
    report = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    with httpx.Client(
        base_url="http://localhost:8000",
        timeout=30,
        headers={"Authorization": "Bearer " + OWNER_TOKEN},
    ) as client:

        def request(method, path, **kwargs):
            """Require the product boundary to acknowledge every request before proceeding."""
            result = client.request(method, path, **kwargs)
            result.raise_for_status()
            return result.json()

        for _ in range(120):
            try:
                if client.get("/v1/health/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("product API not ready")

        def admit(message):
            """Create a product task without choosing any native execution parameters."""
            conversation = request("POST", "/v1/conversations", json={})["conversation_id"]
            return request(
                "POST",
                f"/v1/conversations/{conversation}/turns",
                json={"message": message},
                headers={"Idempotency-Key": str(uuid4())},
            )

        def snapshot(item):
            """Read the same durable Turn after another process has restarted."""
            return request(
                "GET", f"/v1/conversations/{item['conversation_id']}/turns/{item['turn_id']}"
            )

        def wait(item, wanted):
            """Wait for a product state and report a bounded diagnostic on timeout."""
            for _ in range(240):
                value = snapshot(item)
                if value["status"] in wanted:
                    return value
                if value["status"] in {"failed", "blocked", "completed", "cancelled"}:
                    raise AssertionError(value)
                time.sleep(0.25)
            raise AssertionError(value)

        if phase == "queued":
            report = {"queued": admit('/tool calculate {"operation":"add","left":2,"right":3}')}
            value = wait(report["queued"], {"queued"})
            time.sleep(1)
            assert snapshot(report["queued"])["status"] == "queued"
            report["product_api_does_not_execute"] = True
        elif phase == "waits":
            value = wait(report["queued"], {"completed"})
            assert "5" in str(value["output"])
            report["root"] = admit(
                '/tool request_user__clarification {"question":"合成问题：选择哪个市场？"}'
            )
            report["root_wait"] = wait(report["root"], {"waiting"})
            report["nested"] = admit(
                "/workflow portfolio_review "
                + json.dumps(
                    {
                        "portfolio_name": "Stage 10 synthetic portfolio",
                        "positions": [{"symbol": "AAPL", "quantity": "2", "cost_basis": "80"}],
                        "max_snapshot_age_hours": 48,
                    }
                )
            )
            report["nested_wait"] = wait(report["nested"], {"waiting"})
            report["nested_native_hitl"] = True
        elif phase == "resume":
            for name in ("root", "nested"):
                latest = snapshot(report[name])
                item = latest["pending_interactions"][0]
                assert (
                    item["interaction_id"]
                    == report[name + "_wait"]["pending_interactions"][0]["interaction_id"]
                )
                response = {"kind": item["kind"], "revision": item["revision"]}
                if item["kind"] == "approval":
                    response.update(decision="reject", action_hash=item["action_hash"])
                else:
                    response["answer"] = {"text": "AAPL"}
                key = str(uuid4())
                path = "/v1/interactions/" + item["interaction_id"] + "/responses"
                first = request("POST", path, json=response, headers={"Idempotency-Key": key})
                second = request("POST", path, json=response, headers={"Idempotency-Key": key})
                assert first["interaction"] == second["interaction"]
                report[name + "_decision"] = first["interaction"]
            report["waiting_survives_api_worker_restart"] = True
        elif phase == "finish":
            for name in ("root", "nested"):
                final = wait(report[name], {"completed"})
                messages = request(
                    "GET", f"/v1/conversations/{report[name]['conversation_id']}/messages"
                )
                assert len(messages["messages"]) == 2
                report[name + "_final"] = {
                    "status": final["status"],
                    "journal_messages": len(messages["messages"]),
                }
            report["resume_survives_api_worker_restart"] = True
            statuses = {}
            for path in (
                "/threads",
                "/assistants/search",
                "/crons/search",
                "/mcp",
                "/threads/search",
                "/runs",
                "/store/items",
                "/noauth/threads",
                "/a2a/finance_agent",
            ):
                result = client.post(
                    path, json={}, headers={"X-Forwarded-Prefix": "/noauth", "X-Internal": "true"}
                )
                assert result.status_code in {401, 403, 404, 405}, (path, result.status_code)
                statuses[path] = result.status_code
            report["native_external_access"] = statuses
            report["store_maintenance"] = asyncio.run(store_contract())
            report["passed"] = True
            report["quote_fixture"] = (
                "Current synthetic quote timestamp; unchanged product graph/policies"
            )
        else:
            raise ValueError("unknown phase")
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"phase": phase, "passed": True}))


async def store_contract():
    """Verify scoped Store CRUD while the same service identity cannot create a native thread."""
    from financeclaw.shared.memory.namespace import owner_namespace

    client = get_client(
        url="http://localhost:8000",
        api_key=None,
        headers={"Authorization": "Bearer " + SERVICE_TOKEN},
    )
    namespace = (
        *owner_namespace(
            SimpleNamespace(tenant_id="synthetic-tenant", subject_id="synthetic-user")
        ),
        "history",
        "probe",
    )
    try:
        await client.store.put_item(namespace, "probe", {"content": "synthetic"}, index=False)
        assert (await client.store.get_item(namespace, "probe"))["value"]["content"] == "synthetic"
        await client.store.delete_item(namespace, "probe")
        for operation in (
            client.threads.create,
            lambda: client.store.get_item(("financeclaw",), "probe"),
        ):
            try:
                await operation()
            except httpx.HTTPStatusError as error:
                assert error.response.status_code == 403
            else:
                raise AssertionError("maintenance credential exceeded its boundary")
        return {"scoped_crud": True, "thread_denied": True, "unbounded_namespace_denied": True}
    finally:
        await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("queued", "waits", "resume", "finish"))
    run(parser.parse_args().phase)
