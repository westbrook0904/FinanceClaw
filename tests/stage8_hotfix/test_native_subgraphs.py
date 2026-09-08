"""HF-0：原生图恢复、重入、状态隔离和探针环境边界。"""

import ast
import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from langsmith import tracing_context

from experiments.stage8_hotfix.environment import isolated_environment
from experiments.stage8_hotfix.graphs import SCENARIOS, ProbeContext, Recorder, build_graph
from experiments.stage8_hotfix.local_probe import local_scenario, snapshot_dict


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_native_tool_subgraph_roundtrip(scenario):
    """验证模型→工具→子图→原位恢复→顶层完成，包含实际节点调用次数。"""
    result = await local_scenario(scenario)
    assert result["passed"]


@pytest.mark.asyncio
async def test_wrong_interrupt_id_does_not_apply_to_waiting_worker():
    """原生 ID 映射不能把另一个等待位置的回答交给当前子图。"""
    recorder = Recorder()
    graph = build_graph(recorder=recorder, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "wrong-interrupt"}}
    context = ProbeContext("wrong-interrupt")
    with tracing_context(enabled=False):
        await graph.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "scenario": "question",
                                "root_id": context.root_id,
                            }
                        ),
                    }
                ]
            },
            config=config,
            context=context,
        )
        before = snapshot_dict(await graph.aget_state(config, subgraphs=True))
        original = before["tasks"][0]["interrupts"][0]
        await graph.ainvoke(
            Command(resume={"f" * 32: {"scope": "synthetic"}}),
            config=config,
            context=context,
        )
        after = snapshot_dict(await graph.aget_state(config, subgraphs=True))
    assert after["tasks"][0]["interrupts"][0] == original
    assert not any(e["event"] in {"question.applied", "tool.return"} for e in recorder.events)


@pytest.mark.asyncio
async def test_non_interrupt_key_is_a_payload_not_an_id_map():
    """普通字符串键会被原生 resume 当作载荷，必须由应用和工具拒绝。"""
    recorder = Recorder()
    graph = build_graph(recorder=recorder, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "malformed-interrupt-key"}}
    context = ProbeContext("malformed-interrupt-key")
    with tracing_context(enabled=False):
        await graph.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "scenario": "question",
                                "root_id": context.root_id,
                            }
                        ),
                    }
                ]
            },
            config=config,
            context=context,
        )
        with pytest.raises(ValueError, match="synthetic scope answer did not match"):
            await graph.ainvoke(
                Command(resume={"not-a-native-interrupt-id": {"scope": "synthetic"}}),
                config=config,
                context=context,
            )
    assert not any(e["event"] in {"question.applied", "tool.return"} for e in recorder.events)


@pytest.mark.asyncio
async def test_invalid_workflow_decision_never_commits_marker():
    """误把资料回答发给审批时，原生子图校验失败且不进入副作用节点。"""
    recorder = Recorder()
    graph = build_graph(recorder=recorder, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "invalid-decision"}}
    context = ProbeContext("invalid-decision")
    with tracing_context(enabled=False):
        result = await graph.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "scenario": "workflow_approve",
                                "root_id": context.root_id,
                            }
                        ),
                    }
                ]
            },
            config=config,
            context=context,
        )
        identifier = result["__interrupt__"][0].id
        with pytest.raises(ValueError, match="invalid workflow decision"):
            await graph.ainvoke(
                Command(resume={identifier: "not-an-approval"}), config=config, context=context
            )
    assert not any(e["event"] == "effect" for e in recorder.events)


def test_probe_server_environment_does_not_inherit_real_secrets(monkeypatch, tmp_path):
    """真实模型／LangSmith／应用凭据不会被父进程带入隔离 native 服务。"""
    for key in (
        "LANGSMITH_API_KEY",
        "LANGGRAPH_CLOUD_LICENSE_KEY",
        "OPENAI_API_KEY",
        "FINANCECLAW_DATABASE_URL",
        "FINANCECLAW_AGENT_SERVER_SERVICE_TOKEN",
    ):
        monkeypatch.setenv(key, "synthetic-secret-must-not-propagate")
    env = isolated_environment(tmp_path / "events.jsonl")
    assert "synthetic-secret-must-not-propagate" not in env.values()
    assert env["LANGSMITH_TRACING"] == "false"
    assert env["LANGGRAPH_API_DO_NOT_TRACK"] == "true"


def test_worker_graph_has_no_sdk_or_application_delegation_dependency():
    """调用契约禁止通过 SDK／HTTP 或旧应用服务隐藏地重新派发 child。"""
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "experiments/stage8_hotfix/graphs.py").read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    forbidden = ("financeclaw", "langgraph_sdk", "langgraph.pregel.remote", "httpx", "requests")
    assert not [name for name in imports if name.startswith(forbidden)]
