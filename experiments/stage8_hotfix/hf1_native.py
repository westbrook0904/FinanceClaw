"""Reproduce production Tool-to-subgraph execution on a credential-free native HTTP server."""

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx
from langgraph_sdk.client import LangGraphClient

from experiments.stage8_hotfix.environment import ROOT, NativeServer, isolated_environment
from tests.stage8_hotfix.test_production_subgraphs import approval


class HF1Server(NativeServer):
    """Reuse bounded process cleanup without changing the frozen HF-0 sources."""

    def __enter__(self):
        """Start only the candidate root; Worker graphs have no HTTP registrations."""
        self.directory.mkdir(parents=True)
        config = self.directory / "langgraph.json"
        config.write_text(
            json.dumps(
                {
                    "dependencies": [str(ROOT)],
                    "env": {},
                    "graphs": {
                        "finance_agent_v1_5_0": (
                            f"{ROOT}/experiments/stage8_hotfix/hf1_server.py:root"
                        )
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
                    str(config),
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
                env=isolated_environment(self.event_log),
                stdout=self.log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"HF-1 server exited; inspect {self.directory}")
                try:
                    if httpx.get(self.url + "/ok", timeout=1, trust_env=False).is_success:
                        return self
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            raise TimeoutError("HF-1 server startup timed out")
        except BaseException:
            self.close()
            raise


async def probe(directory):
    """Only explicit user answers/approvals create native resumes on this one root."""
    with HF1Server(directory) as server:
        context = json.loads((directory / "hf1-context.json").read_text())
        async with httpx.AsyncClient(base_url=server.url, timeout=30, trust_env=False) as http:
            client = LangGraphClient(http)
            thread = await client.threads.create(thread_id=str(uuid4()))
            thread_id = thread["thread_id"]
            arguments = {
                "input": {"messages": [{"role": "user", "content": "HF-1 synthetic root"}]}
            }
            run_ids, waits = [], []
            for index in range(6):
                async with asyncio.timeout(40):
                    run = await client.runs.create(
                        thread_id,
                        "finance_agent_v1_5_0",
                        **arguments,
                        context=context,
                        metadata={"operation_id": f"hf1-operation-{index}"},
                        durability="sync",
                    )
                    run_ids.append(run["run_id"])
                    await client.runs.join(thread_id, run["run_id"])
                    native = await client.runs.get(thread_id, run["run_id"])
                    state = await client.threads.get_state(thread_id, subgraphs=True)
                assert native["status"] == "success", native["status"]
                interrupts = [
                    item for task in state["tasks"] for item in task.get("interrupts", [])
                ]
                if not interrupts:
                    assert not state["next"] and index == 4
                    assert state["metadata"]["run_id"] == run["run_id"]
                    break
                assert len(interrupts) == 1
                item = interrupts[0]
                value = item["value"]
                waits.append(
                    {
                        "interrupt_id": item["id"],
                        "binding": value,
                        "checkpoint_id": state["checkpoint"]["checkpoint_id"],
                    }
                )
                if value.get("kind") == "user_interaction":
                    response = {
                        "kind": "input",
                        "answer": {"analysis_period": "2026"},
                        "invocation_id": value["invocation_id"],
                    }
                else:
                    response = approval(
                        value, "reject" if value["root_tool_call_id"] == "call-5" else "approve"
                    )
                arguments = {
                    "command": {"resume": {item["id"]: response}},
                    "checkpoint": state["checkpoint"],
                }
            else:
                raise AssertionError("unexpected number of native waits")
            messages = state["values"]["messages"]
            outputs = [json.loads(m["content"]) for m in messages if m["type"] == "tool"]
            assert len(outputs) == 5
            assert [m["tool_call_id"] for m in messages if m["type"] == "tool"] == [
                f"call-{i}" for i in range(1, 6)
            ]
            assert outputs[0]["outcome"] == "success" and outputs[1]["status"] == "completed"
            assert outputs[2]["outcome"] == "answer" and outputs[2]["answer_text"]
            assert outputs[3]["outcome"] == "success" and outputs[4]["status"] == "rejected"
            threads = await client.threads.search(limit=100)
            runs = await client.runs.list(thread_id, limit=100)
            assert len(threads) == 1 and {r["run_id"] for r in runs} == set(run_ids)
            from financeclaw.shared.execution_ledger.repository import ExecutionRepository
            from financeclaw.shared.infrastructure.database import ApplicationDatabase

            db = ApplicationDatabase(f"sqlite+pysqlite:///{directory / 'hf1.db'}")
            try:
                execution = ExecutionRepository(db.session_factory)
                assert len(execution.tree("root")) == 1
                budget = execution.get("root")
            finally:
                db.close()
    return {
        "passed": True,
        "mode": "langgraph_dev_http",
        "registered_graphs": ["finance_agent_v1_5_0"],
        "native_thread_id": thread_id,
        "native_run_ids": run_ids,
        "waits": waits,
        "business_roots": 1,
        "business_children": 0,
        "native_thread_count": len(threads),
        "native_run_count": len(runs),
        "child_http_threads": 0,
        "child_http_runs": 0,
        "root_tool_call_ids": [f"call-{i}" for i in range(1, 6)],
        "public_outcomes": ["success", "completed", "answer", "success", "rejected"],
        "root_budget": {
            key: budget[key] for key in ("model_calls", "tool_calls", "operation_calls")
        },
        "persistent_runtime_restart_verified": False,
    }


def main():
    """Write bounded evidence without user messages, model credentials or service tokens."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="financeclaw-hf1-")) / "native"
    report = asyncio.run(probe(directory))
    paths = [
        *ROOT.glob("financeclaw/agent_server/**/*.py"),
        *ROOT.glob("financeclaw/shared/releases/*.py"),
        ROOT / "tests/stage8_hotfix/test_production_subgraphs.py",
        Path(__file__),
        ROOT / "experiments/stage8_hotfix/hf1_server.py",
    ]
    report["source_sha256"] = {
        str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": True, "report": str(args.report), "logs": str(directory)}))


if __name__ == "__main__":
    main()
