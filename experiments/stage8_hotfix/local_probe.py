"""HF-0 本地原生图验收；每次 resume 重建 Graph 实例，复用原 checkpointer。"""

import json

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from langsmith import tracing_context

from experiments.stage8_hotfix.checks import verify_final, waiting
from experiments.stage8_hotfix.graphs import SCENARIOS, ProbeContext, Recorder, build_graph


def snapshot_dict(snapshot) -> dict:
    """只转换验收需要的公开 StateSnapshot 字段。"""
    return {
        "values": snapshot.values,
        "next": snapshot.next,
        "checkpoint": snapshot.config["configurable"],
        "tasks": [
            {
                "name": t.name,
                "state": t.state,
                "error": t.error,
                "interrupts": [{"id": i.id, "value": i.value} for i in t.interrupts],
            }
            for t in snapshot.tasks
        ],
    }


async def local_scenario(scenario: str) -> dict:
    """模型为合成实现，ToolNode、子图、检查点和 resume 全部使用原生实现。"""
    recorder, saver = Recorder(), InMemorySaver()
    config = {"configurable": {"thread_id": "hf0-local-" + scenario}}
    context = ProbeContext(root_id="hf0-local-" + scenario)
    graph = build_graph(recorder=recorder, checkpointer=saver)
    command = {
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "scenario": scenario,
                        "root_id": context.root_id,
                    }
                ),
            }
        ]
    }
    waits = []
    with tracing_context(enabled=False):
        for _ in range(4):
            await graph.ainvoke(command, config=config, context=context)
            snapshot = snapshot_dict(await graph.aget_state(config, subgraphs=True))
            resume, evidence = waiting(snapshot, scenario)
            if resume is None:
                result = verify_final(scenario, snapshot, recorder.events, waits)
                return {**result, "graph_rebuilds_at_resume": len(waits)}
            waits.append(evidence)
            graph = build_graph(recorder=recorder, checkpointer=saver)
            command = Command(resume=resume)
    raise AssertionError("probe exceeded its bounded resume count")


async def run_local() -> dict:
    """顺序隔离全部原生场景，返回可审阅的计数证据。"""
    return {
        "mode": "native_graph_in_memory",
        "scenarios": [await local_scenario(scenario) for scenario in SCENARIOS],
    }
