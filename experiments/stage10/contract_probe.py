"""Run native contract checks inside the probe network without user data."""

import argparse
import importlib.metadata
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

REPORT = Path("/stage10/native-contract.json")


def run(phase: str) -> dict:
    """Persist phase evidence so container restarts cannot erase assertions."""
    result = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    with httpx.Client(base_url="http://localhost:8000", timeout=30, trust_env=False) as client:
        for _ in range(60):
            try:
                response = client.get("/probe/identity")
                if response.is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        response.raise_for_status()
        api = response.json()

        def request(method: str, path: str, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        if phase == "queued":
            thread = str(uuid4())
            native = request("POST", f"/probe/start/{thread}", json={})
            time.sleep(1)
            current = request("GET", f"/threads/{thread}/runs/{native['run_id']}")
            assert current["status"] == "pending", current
            result.update(
                thread_id=thread, native_run_id=native["run_id"], api_does_not_execute=True
            )
        elif phase == "complete":
            thread, native_id = result["thread_id"], result["native_run_id"]
            native = request("GET", f"/probe/join/{thread}/{native_id}")
            assert native["status"] == "success", native
            state = request("GET", f"/probe/state/{thread}")
            assert state["values"]["answer"] == "done"
            assert state["values"]["worker"] != api["host"]
            result.update(loopback_create_and_join=True, separate_worker_execution=True)
            thread = str(uuid4())
            native = request(
                "POST", f"/probe/start/{thread}", json={"question": "synthetic question"}
            )
            paused = request("GET", f"/probe/join/{thread}/{native['run_id']}")
            assert paused["status"] in {"success", "interrupted"}, paused
            state = request("GET", f"/probe/state/{thread}")
            assert state.get("interrupts") or any(
                t.get("interrupts") for t in state.get("tasks", [])
            )
            result.update(
                interrupt_thread=thread,
                interrupt_checkpoint=state["checkpoint"],
                interrupt_native_status=paused["status"],
            )
        elif phase == "resume":
            thread = result["interrupt_thread"]
            state = request("GET", f"/probe/state/{thread}")
            assert state["checkpoint"] == result["interrupt_checkpoint"]
            native = request(
                "POST",
                f"/probe/resume/{thread}",
                json={
                    "answer": "synthetic answer",
                    "checkpoint": state["checkpoint"],
                },
            )
            final = request("GET", f"/probe/join/{thread}/{native['run_id']}")
            assert final["status"] == "success", final
            state = request("GET", f"/probe/state/{thread}")
            assert state["values"]["answer"] == "synthetic answer"
            result.update(
                checkpoint_survives_api_worker_restart=True, exact_resume=True, passed=True
            )
        result["versions"] = {
            p: importlib.metadata.version(p)
            for p in (
                "langgraph-api",
                "langgraph-sdk",
                "langgraph",
            )
        }
        result["runtime"] = "postgres"
    REPORT.write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["queued", "complete", "resume"])
    print(json.dumps(run(parser.parse_args().phase)))
