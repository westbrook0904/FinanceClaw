"""HF-0 真实 HTTP 验证：只创建顶层 Run，每次用户决定恢复同一个 thread。"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
from langgraph_sdk.client import LangGraphClient

from experiments.stage8_hotfix.checks import require, verify_final, waiting
from experiments.stage8_hotfix.environment import NativeServer
from experiments.stage8_hotfix.graphs import ROOT_GRAPH_ID, SCENARIOS


async def native_scenario(client, server, scenario):
    """只在本函数的明确 start／用户回答边界调用原生创建 API。"""
    root_id = "hf0-http-" + scenario
    thread = await client.threads.create(thread_id=str(uuid4()))
    thread_id = thread["thread_id"]
    waits, run_ids, operations = [], [], []
    native_statuses = []
    arguments = {
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "scenario": scenario,
                            "root_id": root_id,
                        }
                    ),
                }
            ]
        }
    }
    for index in range(4):
        operation = f"{root_id}-operation-{index}"
        async with asyncio.timeout(30):
            run = await client.runs.create(
                thread_id,
                ROOT_GRAPH_ID,
                **arguments,
                metadata={"hf0_root_id": root_id, "operation_id": operation},
                context={"root_id": root_id},
                multitask_strategy="reject",
                durability="sync",
            )
            run_ids.append(run["run_id"])
            operations.append(operation)
            await client.runs.join(thread_id, run["run_id"])
            native_run = await client.runs.get(thread_id, run["run_id"])
            native_statuses.append(native_run["status"])
            snapshot = await client.threads.get_state(thread_id, subgraphs=True)
        # 子图连续中断时父 checkpoint 不前进，metadata.run_id 仍可能是首次尝试。
        # 探针使用独占 thread、原生 Run 回执、固定命令及完整 attempt 清单确认所属链。
        require(snapshot["metadata"]["run_id"] in run_ids, "state belongs to another root")
        require(snapshot["metadata"]["hf0_root_id"] == root_id, "root checkpoint identity changed")
        require(native_run["metadata"]["operation_id"] == operation, "operation binding changed")
        existing = await client.runs.list(thread_id, limit=100)
        require({r["run_id"] for r in existing} == set(run_ids), "unowned attempt on root thread")
        resume, evidence = waiting(snapshot, scenario)
        if resume is None:
            require(native_run["status"] == "success", "native root did not succeed")
            require(
                snapshot["metadata"]["run_id"] == run["run_id"],
                "final output is from another attempt",
            )
            report = verify_final(scenario, snapshot, server.events(root_id), waits)
            existing = await client.runs.list(thread_id, limit=100)
            require(
                {r["run_id"] for r in existing} == set(run_ids), "unexpected child/background Run"
            )
            require(len(existing) == 1 + len(waits), "child completion created an external resume")
            require(
                all(r["metadata"]["operation_id"] in operations for r in existing),
                "unowned native attempt",
            )
            return {
                **report,
                "thread_id": thread_id,
                "native_run_ids": run_ids,
                "native_statuses": native_statuses,
                "native_run_count": len(existing),
                "child_http_runs": 0,
            }
        evidence.update(
            active_native_run_id=run["run_id"],
            checkpoint_source_native_run_id=snapshot["metadata"]["run_id"],
            checkpoint_metadata_matches_active_attempt=snapshot["metadata"]["run_id"]
            == run["run_id"],
            checkpoint_reused_from_previous_wait=bool(waits)
            and waits[-1]["checkpoint_id"] == evidence["checkpoint_id"],
        )
        waits.append(evidence)
        arguments = {"command": {"resume": resume}, "checkpoint": snapshot["checkpoint"]}
    raise AssertionError("native probe exceeded its bounded resume count")


async def run_native(directory: Path) -> dict:
    """在独占服务上盘点全部 thread/run，检测工具内部额外 HTTP 派发。"""
    with NativeServer(directory) as server:
        async with httpx.AsyncClient(base_url=server.url, timeout=30, trust_env=False) as http:
            client = LangGraphClient(http)
            results = [await native_scenario(client, server, scenario) for scenario in SCENARIOS]
            threads = await client.threads.search(limit=100)
            require(
                {t["thread_id"] for t in threads} == {r["thread_id"] for r in results},
                "unexpected child thread",
            )
    return {
        "mode": "langgraph_dev_http",
        "registered_graphs": [ROOT_GRAPH_ID],
        "thread_count": len(threads),
        "native_run_count": sum(r["native_run_count"] for r in results),
        "child_http_threads": 0,
        "child_http_runs": 0,
        "persistent_runtime_restart_verified": False,
        "scenarios": results,
    }
