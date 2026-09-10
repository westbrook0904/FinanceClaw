"""Exercise BFF HTTP admission, human resumes and offline Journal completion against native HTTP."""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from hashlib import sha256
from pathlib import Path

import httpx
import uvicorn
from langgraph_sdk.client import LangGraphClient
from sqlalchemy import select

from experiments.stage8_hotfix.environment import ROOT, NativeServer, isolated_environment
from financeclaw.bff.bootstrap import create_default_app
from financeclaw.shared.execution_ledger.run_tables import (
    RootRunRow,
    RunInboxRow,
)
from tests.stage8_hotfix.test_bff_runs import OWNER, SCOPES, config


class HF2Server(NativeServer):
    """Start one registered root graph with authenticated callbacks to a dedicated BFF port."""

    def __init__(self, directory, bff_port, scenario="mixed"):
        """Allocate only private probe listeners, never attach to an existing deployment."""
        super().__init__(directory)
        self.bff_port = bff_port
        self.scenario = scenario

    def __enter__(self):
        """Launch the credential-free server and bound its startup/cleanup time."""
        self.directory.mkdir(parents=True)
        path = self.directory / "langgraph.json"
        path.write_text(
            json.dumps(
                {
                    "dependencies": [str(ROOT)],
                    "env": {},
                    "graphs": {
                        "finance_agent_v1_5_0": (
                            f"{ROOT}/experiments/stage8_hotfix/hf2_server.py:root"
                        )
                    },
                    "webhooks": {
                        "headers": {
                            "Authorization": ("Bearer synthetic-bff-webhook-token-at-least-32")
                        },
                        "url": {
                            "allowed_domains": ["127.0.0.1"],
                            "allowed_ports": [self.bff_port],
                            "require_https": False,
                            "disable_loopback": False,
                        },
                    },
                }
            )
        )
        self.log = (self.directory / "agent-server.log").open("w")
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "langgraph_cli",
                    "dev",
                    "--config",
                    str(path),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--no-browser",
                    "--no-reload",
                    "--allow-blocking",
                    "--server-log-level",
                    "WARNING",
                ],
                cwd=self.directory,
                env={
                    **isolated_environment(self.event_log),
                    "HF2_SCENARIO": self.scenario,
                },
                stdout=self.log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"HF-2 server exited; inspect {self.directory}")
                try:
                    if httpx.get(self.url + "/ok", timeout=1, trust_env=False).is_success:
                        return self
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            raise TimeoutError("HF-2 Agent Server startup timed out")
        except BaseException:
            self.close()
            raise


def build_probe_app(settings, scenario):
    """Inject only the synthetic HITL publication; normal probes use untouched BFF bootstrap."""
    if scenario == "mixed":
        return create_default_app(settings)
    from unittest.mock import patch

    from experiments.stage8_hotfix.hf2_hitl_release import with_test_hitl
    from financeclaw.shared.releases.catalog import build_release_catalogs

    def catalogs(*args, **kwargs):
        """Apply the same isolated synthetic declaration used in the native server."""
        return with_test_hitl(build_release_catalogs(*args, **kwargs))

    with patch("financeclaw.bff.application.runs.bootstrap.build_release_catalogs", catalogs):
        return create_default_app(settings)


async def start_bff(settings, port, scenario="mixed"):
    """Run the production bootstrap and lifespan on a private loopback HTTP listener."""
    app = build_probe_app(settings, scenario)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(15):
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("BFF did not start")
            await asyncio.sleep(0.02)
    return app, server, task


async def stop_bff(server, task):
    """Close only the probe's BFF and leave accepted native executions untouched."""
    server.should_exit = True
    await asyncio.wait_for(task, 10)


async def probe(directory, scenario="mixed"):
    """Use one Turn, four human resumes, BFF reconstruction and no completion GET/SSE."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        bff_port = sock.getsockname()[1]
    native = HF2Server(directory, bff_port, scenario)
    await asyncio.to_thread(native.__enter__)
    app = server = task = None
    try:
        settings = config(directory).model_copy(
            update={
                "agent_server_url": native.url,
                "bff_callback_url": f"http://127.0.0.1:{bff_port}/internal/webhooks/langgraph/langgraph-main",
                "bff_auth_token": __import__("pydantic").SecretStr("synthetic-client"),
                "bff_tenant_id": OWNER["tenant_id"],
                "bff_subject_id": OWNER["subject_id"],
                "bff_scopes": SCOPES,
                "log_level": "WARNING",
            }
        )
        app, server, task = await start_bff(settings, bff_port, scenario)
        runtime = app.state.financeclaw_bff_runs
        url = f"http://127.0.0.1:{bff_port}"
        headers = {"Authorization": "Bearer synthetic-client"}
        async with httpx.AsyncClient(
            base_url=url, headers=headers, timeout=10, trust_env=False
        ) as http:
            response = await http.post("/v1/conversations", json={})
            response.raise_for_status()
            conversation_id = response.json()["conversation_id"]
            response = await http.post(
                f"/v1/conversations/{conversation_id}/turns",
                headers={"Idempotency-Key": "hf2-native-turn"},
                json={"message": "HF-2 synthetic multi-worker turn"},
            )
            response.raise_for_status()
            run_id = response.json()["run_id"]
        waits = []
        wait_count = 2 if scenario == "hitl" else 4
        for index in range(wait_count):
            async with asyncio.timeout(45):
                while True:
                    # Read durable facts for assertions; these queries never reconcile.
                    with runtime.runs.store.sessions() as session:
                        row = session.get(RootRunRow, run_id)
                        projection, error = dict(row.projection), row.last_error
                    if projection.get("pending_interactions"):
                        break
                    if error not in {None, "BFFShutdown"} or projection["status"] in {
                        "failed",
                        "cancelled",
                    }:
                        raise AssertionError((projection, error, directory))
                    await asyncio.sleep(0.1)
            item = projection["pending_interactions"][0]
            waits.append(
                {
                    "interaction_id": item["interaction_id"],
                    "revision": item["revision"],
                    "kind": item["kind"],
                }
            )
            # Rebuild the BFF between questions using only the shared database.
            if index == 1:
                await stop_bff(server, task)
                app, server, task = await start_bff(settings, bff_port, scenario)
                runtime = app.state.financeclaw_bff_runs
            body = {"revision": item["revision"], "kind": item["kind"]}
            if item["kind"] == "input":
                body["answer"] = {"analysis_period": "2026"}
            else:
                body.update(
                    decision="reject" if index == wait_count - 1 else "approve",
                    action_hash=item["action_hash"],
                )
            async with httpx.AsyncClient(
                base_url=url, headers=headers, timeout=10, trust_env=False
            ) as http:
                answer = await http.post(
                    item["response_url"],
                    headers={"Idempotency-Key": f"hf2-answer-{index}"},
                    json=body,
                )
                answer.raise_for_status()
                replay = await http.post(
                    item["response_url"],
                    headers={"Idempotency-Key": f"hf2-answer-{index}"},
                    json=body,
                )
                replay.raise_for_status()
        # All client HTTP connections are closed. Only the BFF background observer remains.
        async with asyncio.timeout(45):
            while True:
                messages = runtime.runs.repository.list_messages(conversation_id)
                if len(messages) == 2:
                    break
                with runtime.runs.store.sessions() as session:
                    row = session.get(RootRunRow, run_id)
                    if row.last_error not in {None, "BFFShutdown"}:
                        raise AssertionError((row.projection, row.last_error, directory))
                await asyncio.sleep(0.1)
        assert messages[-1].content == "all workers finished"
        snapshot = runtime.runs.execution.get(run_id)["snapshot"]
        async with httpx.AsyncClient(base_url=native.url, timeout=20, trust_env=False) as http:
            sdk = LangGraphClient(http)
            threads = await sdk.threads.search(limit=100)
            runs = await sdk.runs.list(snapshot["thread_id"], limit=100)
            state = await sdk.threads.get_state(snapshot["thread_id"])
        assert len(threads) == 1 and len(runs) == wait_count + 1
        assert runtime.runs.execution.get(run_id)["root_run_id"] == run_id
        outputs = [
            json.loads(m["content"]) for m in state["values"]["messages"] if m["type"] == "tool"
        ]
        expected = (
            ["partial", "partial"]
            if scenario == "hitl"
            else ["success", "completed", "answer", "rejected"]
        )
        assert [value.get("outcome", value.get("status")) for value in outputs] == expected
        with runtime.runs.store.sessions() as session:
            callbacks = list(
                session.scalars(
                    select(RunInboxRow).where(RunInboxRow.kind == "backend_notification")
                )
            )
            root = session.get(RootRunRow, run_id)
            assert root.driver_version == 1 and not root.active and callbacks
        execution = runtime.runs.execution.get(run_id)
        return {
            "passed": True,
            "mode": "production_bff_and_native_agent_server_http",
            "registered_graphs": ["finance_agent_v1_5_0"],
            "business_roots": 1,
            "business_children": 0,
            "native_thread_count": len(threads),
            "native_run_count": len(runs),
            "human_resumes": wait_count,
            "scenario": scenario,
            "waits": waits,
            "bff_reconstructed_from_database": True,
            "journal_messages": len(messages),
            "finalized_with_no_client_connections": True,
            "authenticated_webhook_count": len(callbacks),
            "driver_version": 1,
            "root_budget": {
                k: execution[k] for k in ("model_calls", "tool_calls", "operation_calls")
            },
            "public_outcomes": expected,
            "rejection_applied_in_graph": execution["side_effects_denied"],
            "persistent_agent_server_restart_verified": False,
        }
    finally:
        if task is not None and not task.done():
            await stop_bff(server, task)
        await asyncio.to_thread(native.close)


def main():
    """Save bounded reproducible evidence without native question content or client credentials."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--scenario", choices=("mixed", "hitl"), default="mixed")
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="financeclaw-hf2-")) / "native"
    clean = isolated_environment(directory / "events.jsonl")
    os.environ.clear()
    os.environ.update(clean)
    report = asyncio.run(probe(directory, args.scenario))
    paths = [
        *ROOT.glob("financeclaw/bff/**/*.py"),
        *ROOT.glob("financeclaw/agent_server/**/*.py"),
        *ROOT.glob("financeclaw/kernel/*.py"),
        *ROOT.glob("financeclaw/shared/releases/*.py"),
        *ROOT.glob("financeclaw/shared/execution_ledger/*.py"),
        *ROOT.glob("financeclaw/shared/backends/*.py"),
        ROOT / "financeclaw/shared/infrastructure/migrations/versions/0001_initial.py",
        *ROOT.glob("experiments/stage8_hotfix/hf2_*.py"),
    ]
    report["source_sha256"] = {
        str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in sorted(paths)
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": True, "report": str(args.report), "logs": str(directory)}))


if __name__ == "__main__":
    main()
