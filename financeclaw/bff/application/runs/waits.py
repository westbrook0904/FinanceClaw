"""Map native root waits against the frozen root/Worker release, without child runs."""

import json
from datetime import datetime, timedelta

from financeclaw.bff.application.runs.backend import current_messages, interrupts
from financeclaw.kernel.interactions import InteractionPoint
from financeclaw.shared.execution_ledger.repository import ExecutionConflict, digest
from financeclaw.shared.execution_ledger.root_repository import now


def pending_call(state, snapshot):
    """Use native ToolMessage pairing, never model text, to identify the outer pending Tool."""
    messages = current_messages(state, snapshot)
    returned = {m.get("tool_call_id") for m in messages if m.get("type") == "tool"}
    calls = [
        call
        for message in messages
        for call in message.get("tool_calls", [])
        if call.get("id") not in returned
    ]
    if len(calls) != 1 or not calls[0].get("id"):
        raise ExecutionConflict("human interrupt has no unique pending root Tool")
    return calls[0]


def worker_binding(call, snapshot, releases):
    """Reconstruct the code-owned invocation digest using the exact wrapper input defaults."""
    entries = [(raw, json.loads(raw)) for raw in snapshot["profile"]["worker_manifest"]]
    matches = [(raw, item) for raw, item in entries if item["tool_id"] == call["name"]]
    if not matches:
        return None, None, None
    if len(matches) != 1:
        raise ExecutionConflict("ambiguous Worker release in root manifest")
    raw, entry = matches[0]
    arguments = call["args"]
    if entry["kind"] == "agent":
        if set(arguments) - {"task", "arguments", "context_refs"}:
            raise ExecutionConflict("Worker invocation includes undeclared arguments")
        normalized = {
            "task": arguments["task"],
            "arguments": arguments.get("arguments", {}),
            "context_refs": list(arguments.get("context_refs", [])),
        }
    else:
        definition = releases.workflows.resolve(entry["target_id"], entry["version"])
        normalized = definition.input_schema.model_validate(arguments).model_dump(mode="json")
    binding = {
        "root_run_id": snapshot["context"]["run_id"],
        "root_tool_call_id": call["id"],
        "worker_kind": entry["kind"],
        "worker_id": entry["target_id"],
        "worker_version": entry["version"],
        "invocation_id": digest(
            [snapshot["context"]["run_id"], call["id"], raw, digest(normalized)]
        ),
    }
    return entry, binding, normalized


def map_wait(observation, snapshot, releases, settings):
    """Validate question, HITL or Workflow approval against code-pinned declarations."""
    releases.verify(snapshot)
    state = observation["state"]
    native = interrupts(state)[0]
    payload = native["value"]
    if not isinstance(payload, dict) or len(json.dumps(payload).encode()) > 65536:
        raise ExecutionConflict("native human request is not a bounded object")
    call = pending_call(state, snapshot)
    worker, invocation, arguments = worker_binding(call, snapshot, releases)
    profile = worker["profile"] if worker else snapshot["profile"]
    source, action, allowed = None, None, ["approve", "reject"]
    if "action_requests" in payload:
        source = "hitl"
        actions, configs = payload.get("action_requests", []), payload.get("review_configs", [])
        if len(actions) != 1 or len(configs) != 1:
            raise ExecutionConflict("only one native HITL action is supported")
        action, config = actions[0], configs[0]
        if worker:
            leaves = worker["tools"]
        else:
            leaves = [
                releases.tools.resolve(ref["tool_id"], ref["version"]).governance.model_dump(
                    mode="json"
                )
                for ref in profile["allowed_tools"]
            ]
            if action.get("name") != call["name"] or action.get("args") != call["args"]:
                raise ExecutionConflict("root approval action differs from pending Tool")
        leaf = next((item for item in leaves if item["tool_id"] == action.get("name")), None)
        if (
            leaf is None
            or leaf["approval"] != "always"
            or config.get("action_name") != action["name"]
        ):
            raise ExecutionConflict("native HITL action is not a pinned approval leaf")
        allowed = [d for d in config.get("allowed_decisions", []) if d in {"approve", "reject"}]
        if not allowed or not isinstance(action.get("args"), dict):
            raise ExecutionConflict("native HITL has no supported action or decision")
        point = InteractionPoint(
            point_id="tool_approval",
            kind="approval",
            question=f"请确认是否执行 {action['name']}",
            required_scope=settings.bff_approval_scope,
            timeout_seconds=settings.approval_timeout_seconds,
        )
    else:
        if worker and any(payload.get(key) != value for key, value in invocation.items()):
            raise ExecutionConflict("native request does not match the root Worker invocation")
        if payload.get("kind") == "user_interaction":
            source = "question"
            declared = next(
                (
                    p
                    for p in profile.get("interaction_points", [])
                    if p["point_id"] == payload.get("point_id")
                ),
                None,
            )
            if declared is None or payload.get("schema_version") != 1:
                raise ExecutionConflict("unpublished native question")
            point = InteractionPoint.model_validate(declared)
            if point.kind != payload.get("interaction_kind"):
                raise ExecutionConflict("native question differs from pinned kind")
            if not worker and call["name"] != "request_user__" + point.point_id:
                raise ExecutionConflict("root question differs from pending Tool")
            action = payload.get("action") if point.kind == "approval" else None
        elif worker and worker["kind"] == "workflow" and payload.get("approval_id"):
            source = "workflow"
            declaration = next(
                (
                    p
                    for p in profile["approval_points"]
                    if p["approval_id"] == payload.get("approval_point")
                ),
                None,
            )
            if declaration is None or any(
                (
                    payload.get("workflow_id") != worker["target_id"],
                    payload.get("workflow_version") != worker["version"],
                    payload.get("arguments_hash") != digest(arguments),
                    payload.get("required_scope") != declaration["required_scope"],
                    payload.get("allowed_decisions") != declaration["allowed_decisions"],
                    payload.get("requested_action") != declaration["requested_action"],
                )
            ):
                raise ExecutionConflict("Workflow approval differs from the pinned invocation")
            identity = {
                **invocation,
                "run_id": invocation["root_run_id"],
                "workflow_id": worker["target_id"],
                "workflow_version": worker["version"],
                "approval_point": declaration["approval_id"],
                "arguments_hash": digest(arguments),
            }
            if payload["approval_id"] != "approval-" + digest(identity)[:24]:
                raise ExecutionConflict("Workflow approval identity differs from this invocation")
            allowed, action = declaration["allowed_decisions"], payload
            point = InteractionPoint(
                point_id="workflow_approval",
                kind="approval",
                question=f"请确认 {declaration['requested_action']}",
                required_scope=declaration["required_scope"],
                timeout_seconds=profile["timeout_policy"]["approval_timeout_seconds"],
            )
        else:
            raise ExecutionConflict("unsupported root interrupt")
    question = payload.get("question") or point.question
    if not isinstance(question, str) or not 1 <= len(question) <= 2000:
        raise ExecutionConflict("native question must be bounded")
    if point.kind == "approval" and not action:
        raise ExecutionConflict("approval requires a concrete action")
    expires = now() + timedelta(seconds=point.timeout_seconds)
    if source == "workflow":
        native_expiry = datetime.fromisoformat(payload["expires_at"])
        if native_expiry.tzinfo is None or native_expiry > expires:
            raise ExecutionConflict("Workflow approval expiry exceeds its release")
        expires = native_expiry
    return {
        "source": source,
        "point": point.model_dump(mode="json"),
        "question": question,
        "action": action,
        "action_hash": digest(action) if action is not None else None,
        "allowed_decisions": allowed,
        "invocation": invocation,
        "expires_at": expires.isoformat(),
        "binding": {
            "native_run_id": observation["native_run_id"],
            "anchor_run_id": state["metadata"]["run_id"],
            "checkpoint": state["checkpoint"],
            "interrupt_id": native["id"],
            "interrupt_hash": digest(payload),
        },
    }


def native_response(request, response):
    """Translate only a validated human answer into native resume data."""
    invocation = request["invocation"]
    if request["source"] in {"workflow", "hitl"}:
        decision = {"type": response.decision}
        if response.reason:
            decision["message"] = response.reason
        if request["source"] == "workflow":
            decision.update(
                arguments_hash=request["action"]["arguments_hash"],
                approval_id=request["action"]["approval_id"],
                invocation_id=invocation["invocation_id"],
            )
            return decision
        return {"decisions": [decision]}
    result = response.model_dump(mode="json", exclude={"revision"}, exclude_none=True)
    if invocation:
        result["invocation_id"] = invocation["invocation_id"]
    return result
