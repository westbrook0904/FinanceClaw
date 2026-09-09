"""本地原生图与 HTTP 探针共享的行为验收，独立验证原生子图行为。"""

import json
from collections import Counter

from experiments.stage8_hotfix.graphs import scenario_calls


def require(condition: bool, message: str) -> None:
    """运行探针时也始终执行断言，不受 Python optimize 开关影响。"""
    if not condition:
        raise AssertionError(message)


def messages_as_dict(values: dict) -> list[dict]:
    """统一本地消息对象和 Agent Server JSON 消息。"""
    return [m if isinstance(m, dict) else m.model_dump() for m in values["messages"]]


def waiting(snapshot: dict, scenario: str) -> tuple[dict | None, dict | None]:
    """只用顶层公开 state 定位原 interrupt，不深入不可见的工具子图。"""
    tasks = snapshot.get("tasks", [])
    interrupts = [i for task in tasks for i in task.get("interrupts", [])]
    if not interrupts:
        return None, None
    require(len(interrupts) == 1, "expected exactly one native user wait")
    current = interrupts[0]
    payload = current["value"]
    require(bool(current["id"]), "top-level interrupt must expose an addressable ID")
    messages = messages_as_dict(snapshot["values"])
    returned_ids = {m["tool_call_id"] for m in messages if m["type"] == "tool"}
    pending = [
        call for m in messages for call in m.get("tool_calls", []) if call["id"] not in returned_ids
    ]
    require(len(pending) == 1, "the root must expose one unreturned composite tool call")
    outer = pending[0]
    require(outer["name"] in {"research", "review", "approved_agent"}, "wrong root tool")
    require(
        not any(t.get("state") for t in tasks), "tool subgraph visibility changed; review binding"
    )
    if "action_requests" in payload:
        require(scenario.startswith("hitl_"), "unexpected native HITL")
        require(outer["name"] == "approved_agent", "HITL bound to wrong root tool")
        actions = payload["action_requests"]
        require(len(actions) == 1 and actions[0]["name"] == "commit_marker", "wrong HITL action")
        require(actions[0]["args"] == {"label": outer["args"]["label"]}, "wrong HITL arguments")
        response = {"decisions": [{"type": "reject" if scenario.endswith("reject") else "approve"}]}
        kind = "native_hitl"
    else:
        require(
            payload.get("kind") in {"user_interaction", "workflow_approval"},
            "unexpected native interrupt",
        )
        require(payload["root_call_id"] == outer["id"], "payload bound to another Tool call")
        require(payload["label"] == outer["args"]["label"], "payload bound to another invocation")
        require(
            payload["revision"] == 1 and payload["deadline"] == "2099-01-01T00:00:00Z",
            "unstable wait",
        )
        kind = payload["kind"]
        response = (
            {"scope": "synthetic"}
            if kind == "user_interaction"
            else "reject"
            if scenario.endswith("reject")
            else "approve"
        )
    evidence = {
        "interrupt_id": current["id"],
        "kind": kind,
        "root_tool_call_id": outer["id"],
        "root_tool_name": outer["name"],
        "payload_keys": sorted(payload),
        "nested_state_visible": False,
        "checkpoint_id": snapshot["checkpoint"]["checkpoint_id"],
    }
    require(bool(evidence["checkpoint_id"]), "root checkpoint is missing")
    return {current["id"]: response}, evidence


def expected_waits(scenario: str) -> int:
    """根据验收场景固定原生 resume 次数。"""
    return 0 if scenario == "serial" else 2 if scenario == "two_questions" else 1


def verify_final(scenario: str, snapshot: dict, events: list[dict], waits: list[dict]) -> dict:
    """核对真实副作用／节点次数、工具配对、状态隔离和顶层最终输出。"""
    require(not snapshot.get("next"), "root graph still has pending nodes")
    require(
        not any(t.get("interrupts") or t.get("error") for t in snapshot.get("tasks", [])),
        "root is not complete",
    )
    messages = messages_as_dict(snapshot["values"])
    require(
        messages[-1]["type"] == "ai" and not messages[-1].get("tool_calls"),
        "not a root final AIMessage",
    )
    final = json.loads(messages[-1]["content"])
    plans = scenario_calls(scenario)
    require(final["kind"] == "root_final", "child output was mistaken for root completion")
    expected_ids = [f"root-call-{i + 1}" for i in range(len(plans))]
    returned = [m for m in messages if m["type"] == "tool"]
    require([m["tool_call_id"] for m in returned] == expected_ids, "ToolMessage pairing changed")
    require([m["name"] for m in returned] == [p["name"] for p in plans], "wrong root tool returned")
    require(
        [r["label"] for r in final["results"]] == [p["label"] for p in plans],
        "subgraph state leaked across calls",
    )
    require(len(waits) == expected_waits(scenario), "unexpected number of top-level resumes")
    require(
        len({w["interrupt_id"] for w in waits}) == len(waits),
        "distinct questions reused an interrupt ID",
    )
    counts = Counter(e["event"] for e in events)
    effects = 1 if scenario in {"workflow_approve", "hitl_approve", "repeat_workflow"} else 0
    require(counts["effect"] == effects, "synthetic action executed before approval or repeated")
    require(counts["tool.return"] == len(plans), "a composite tool returned twice")
    require(counts["tool.enter"] == len(plans) + len(waits), "unexpected wrapper replay count")
    reads = sum(p["name"] == "research" and p["mode"] == "plain" for p in plans)
    questions = sum(
        2 if p["mode"] == "two_questions" else 1 if p["mode"] == "question" else 0 for p in plans
    )
    reviews = sum(p["name"] == "review" for p in plans)
    approvals = sum(p["name"] == "review" and p["mode"] == "approval" for p in plans)
    require(counts["read"] == reads, "a completed child read replayed")
    require(counts["question.enter"] == 2 * questions, "question node replay differs")
    require(counts["question.applied"] == questions, "answer application repeated")
    require(counts["workflow.prepare"] == reviews, "completed pre-approval node replayed")
    require(counts["workflow.approval.enter"] == reviews + approvals, "approval replay differs")
    require(counts["workflow.finish"] == reviews, "workflow completion repeated")
    root_rounds = [
        e["result_count"] for e in events if e["event"] == "model" and e["role"] == "root"
    ]
    require(root_rounds == list(range(len(plans) + 1)), "root replanned before its tool returned")
    require(
        all(e["human_count"] == 1 for e in events if e["event"] == "model"),
        "worker inherited root conversation",
    )
    for index in expected_ids:
        entries = [e for e in events if e["event"] == "tool.enter" and e["call_id"] == index]
        require(
            len({e["namespace"] for e in entries}) == 1, "resume changed original tool namespace"
        )
    namespaces = {e["namespace"] for e in events if e["event"] == "tool.enter"}
    require(len(namespaces) == len(plans), "separate tool calls shared a checkpoint namespace")
    return {
        "scenario": scenario,
        "passed": True,
        "root_tool_call_ids": expected_ids,
        "root_model_result_counts": root_rounds,
        "top_level_resume_count": len(waits),
        "waits": waits,
        "event_counts": dict(sorted(counts.items())),
        "synthetic_effect_count": effects,
        "distinct_tool_namespaces": len(namespaces),
        "root_final_kind": final["kind"],
    }
