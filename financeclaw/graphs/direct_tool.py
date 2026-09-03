"""Fixed DirectToolGraph with validation, authorization, approval and projection."""

from typing import Any, Literal, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy, interrupt

from financeclaw.agents.middleware import canonical_arguments_hash, trace_tool_authorization
from financeclaw.artifacts import ArtifactService
from financeclaw.audit import AuditEventType, AuditRecord, AuditRepository
from financeclaw.contracts import DirectToolResponse, ExecutionContext
from financeclaw.tools import (
    SideEffect,
    ToolCatalog,
    ToolDecisionType,
    ToolPolicy,
    TransientToolError,
)


class DirectToolInput(TypedDict):
    tool_id: str
    version: str | None
    arguments: dict[str, Any]


class DirectToolState(DirectToolInput, total=False):
    resolved_version: str
    normalized_arguments: dict[str, Any]
    arguments_hash: str
    decision: dict[str, Any]
    approval_id: str
    approved_hash: str
    approval_outcome: Literal["approved", "rejected", "edited", "invalidated"]
    result: Any
    error: str
    response: dict[str, Any]
    artifact: dict[str, Any]


class DirectToolOutput(TypedDict):
    response: dict[str, Any]


def _parse_decision(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("approval resume payload must be an object")
    decisions = value.get("decisions")
    if isinstance(decisions, list):
        if len(decisions) != 1 or not isinstance(decisions[0], dict):
            raise ValueError("approval resume requires exactly one decision")
        return dict(decisions[0])
    return dict(value)


def _approval_id(context: ExecutionContext, state: DirectToolState) -> str:
    source = (
        f"{context.run_id}:{state['tool_id']}:{state['resolved_version']}:{state['arguments_hash']}"
    )
    return f"approval-{canonical_arguments_hash({'source': source})[:24]}"


def build_direct_tool_graph(
    *,
    catalog: ToolCatalog,
    policy: ToolPolicy,
    audit: AuditRepository,
    checkpointer: Any = None,
    read_max_attempts: int = 3,
    artifact_service: ArtifactService | None = None,
) -> Any:
    """Compile the deterministic graph; only the READ node has a retry policy."""

    def append_audit(
        context: ExecutionContext,
        state: DirectToolState,
        *,
        event: AuditEventType,
        decision: str,
        tool_call_id: str | None = None,
    ) -> None:
        managed = catalog.resolve(state["tool_id"], state["resolved_version"])
        audit.append(
            AuditRecord(
                event_type=event,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                conversation_id=context.conversation_id,
                turn_id=context.turn_id,
                run_id=context.run_id,
                tool_call_id=tool_call_id,
                resource_id=managed.governance.tool_id,
                resource_version=managed.governance.version,
                action=managed.governance.side_effect.value,
                decision=decision,
                policy_version=policy.version,
                payload_hash=state["arguments_hash"],
                artifact_refs=(
                    (str(state["artifact"]["artifact_id"]),) if state.get("artifact") else ()
                ),
                metadata={
                    "risk_level": managed.governance.risk_level.value,
                    "approval_id": state.get("approval_id"),
                },
            )
        )

    def validate_target(state: DirectToolState) -> dict[str, Any]:
        try:
            managed = catalog.resolve(state["tool_id"], state.get("version"))
            if not managed.governance.direct_invocation:
                raise ValueError("tool is not available through DirectToolGraph")
            schema = managed.tool.get_input_schema()
            normalized = schema.model_validate(state.get("arguments", {})).model_dump(mode="json")
        except Exception as exc:
            return {"error": f"invalid direct tool target: {exc}"}
        return {
            "resolved_version": managed.governance.version,
            "normalized_arguments": normalized,
            "arguments": normalized,
            "arguments_hash": canonical_arguments_hash(normalized),
            "error": "",
            "approved_hash": "",
        }

    def authorize(state: DirectToolState, runtime: Runtime[ExecutionContext]) -> dict[str, Any]:
        if state.get("error"):
            return {}
        context = runtime.context
        managed = catalog.resolve(state["tool_id"], state["resolved_version"])
        decision = policy.evaluate(context, managed.governance, state["normalized_arguments"])
        trace_tool_authorization(
            tool_id=managed.governance.tool_id,
            effect=decision.effect.value,
            arguments_hash=state["arguments_hash"],
            context_metadata=context.trace_metadata(),
        )
        event = {
            ToolDecisionType.ALLOW: AuditEventType.TOOL_ALLOWED,
            ToolDecisionType.DENY: AuditEventType.TOOL_DENIED,
            ToolDecisionType.REQUIRE_APPROVAL: AuditEventType.TOOL_APPROVAL_REQUESTED,
        }[decision.effect]
        approval_id = (
            _approval_id(context, state)
            if decision.effect is ToolDecisionType.REQUIRE_APPROVAL
            else ""
        )
        updated: DirectToolState = dict(state)
        updated["approval_id"] = approval_id
        append_audit(context, updated, event=event, decision=decision.effect.value)
        return {"decision": decision.model_dump(mode="json"), "approval_id": approval_id}

    def route_authorization(state: DirectToolState) -> str:
        if state.get("error"):
            return "project_response"
        effect = state["decision"]["effect"]
        if effect == ToolDecisionType.DENY.value:
            return "project_response"
        if effect == ToolDecisionType.REQUIRE_APPROVAL.value:
            return "approval"
        managed = catalog.resolve(state["tool_id"], state["resolved_version"])
        return (
            "execute_read" if managed.governance.side_effect is SideEffect.READ else "execute_write"
        )

    def approval(state: DirectToolState, runtime: Runtime[ExecutionContext]) -> dict[str, Any]:
        payload = {
            "approval_id": state["approval_id"],
            "tool_id": state["tool_id"],
            "tool_version": state["resolved_version"],
            "arguments": state["normalized_arguments"],
            "arguments_hash": state["arguments_hash"],
            "allowed_decisions": ["approve", "edit", "reject"],
        }
        decision = _parse_decision(interrupt(payload))
        decision_type = decision.get("type")
        if decision_type == "reject":
            append_audit(
                runtime.context,
                state,
                event=AuditEventType.TOOL_REJECTED,
                decision="rejected",
            )
            return {
                "approval_outcome": "rejected",
                "error": str(decision.get("message") or decision.get("reason") or "tool rejected"),
            }
        if decision_type == "edit":
            edited_action = decision.get("edited_action")
            if isinstance(edited_action, dict):
                if edited_action.get("name", state["tool_id"]) != state["tool_id"]:
                    return {
                        "approval_outcome": "rejected",
                        "error": "edited tool name cannot change",
                    }
                arguments = edited_action.get("args")
            else:
                arguments = decision.get("arguments")
            if not isinstance(arguments, dict):
                return {"approval_outcome": "rejected", "error": "edited arguments are required"}
            return {
                "approval_outcome": "edited",
                "arguments": arguments,
                "approved_hash": "",
                "error": "",
            }
        if decision_type != "approve":
            return {"approval_outcome": "rejected", "error": "unsupported approval decision"}
        supplied_hash = decision.get("arguments_hash")
        if supplied_hash is not None and supplied_hash != state["arguments_hash"]:
            return {
                "approval_outcome": "invalidated",
                "approved_hash": "",
                "error": "",
            }
        append_audit(
            runtime.context,
            state,
            event=AuditEventType.TOOL_APPROVED,
            decision="approved",
        )
        return {
            "approval_outcome": "approved",
            "approved_hash": state["arguments_hash"],
            "error": "",
        }

    def route_approval(state: DirectToolState) -> str:
        outcome = state.get("approval_outcome")
        if outcome in {"edited", "invalidated"}:
            return "validate_target"
        if outcome == "rejected" or state.get("error"):
            return "project_response"
        managed = catalog.resolve(state["tool_id"], state["resolved_version"])
        return (
            "execute_read" if managed.governance.side_effect is SideEffect.READ else "execute_write"
        )

    def authorize_execution(state: DirectToolState, context: ExecutionContext) -> tuple[Any, Any]:
        managed = catalog.resolve(state["tool_id"], state["resolved_version"])
        decision = policy.evaluate(context, managed.governance, state["normalized_arguments"])
        if decision.effect is ToolDecisionType.DENY:
            raise PermissionError(decision.reason)
        if (
            decision.effect is ToolDecisionType.REQUIRE_APPROVAL
            and state.get("approved_hash") != state["arguments_hash"]
        ):
            raise PermissionError("approval is missing or no longer matches the arguments")
        return managed, decision

    def execute(state: DirectToolState, runtime: Runtime[ExecutionContext]) -> dict[str, Any]:
        managed, decision = authorize_execution(state, runtime.context)
        try:
            result = managed.tool.invoke(state["normalized_arguments"])
        except Exception:
            append_audit(
                runtime.context,
                state,
                event=AuditEventType.FINANCIAL_TOOL_FAILED,
                decision="failed",
                tool_call_id=f"direct-{uuid4().hex}",
            )
            raise
        artifact = None
        if artifact_service is not None:
            result, metadata = artifact_service.offload(
                result,
                context=runtime.context,
                source_type="direct_tool_result",
                source_id=managed.governance.tool_id,
            )
            if metadata is not None:
                artifact = {
                    "artifact_id": metadata.artifact_id,
                    "content_type": metadata.content_type,
                    "content_hash": metadata.content_hash,
                    "size_bytes": metadata.size_bytes,
                }
        audit_state: DirectToolState = dict(state)
        if artifact is not None:
            audit_state["artifact"] = artifact
        append_audit(
            runtime.context,
            audit_state,
            event=AuditEventType.FINANCIAL_TOOL_EXECUTED,
            decision="executed",
            tool_call_id=f"direct-{uuid4().hex}",
        )
        return {
            "result": result,
            "artifact": artifact,
            "decision": decision.model_dump(mode="json"),
            "error": "",
        }

    def project_response(
        state: DirectToolState, runtime: Runtime[ExecutionContext]
    ) -> dict[str, Any]:
        decision = state.get("decision", {})
        effect = decision.get("effect")
        if state.get("result") is not None:
            status = "success"
        elif state.get("approval_outcome") == "rejected":
            status = "rejected"
        elif effect == ToolDecisionType.DENY.value:
            status = "denied"
        else:
            status = "failed"
        version = state.get("resolved_version") or state.get("version") or "unknown"
        response = DirectToolResponse(
            run_id=runtime.context.run_id,
            tool_id=state.get("tool_id", "unknown"),
            tool_version=version,
            status=status,
            result=state.get("result"),
            error=state.get("error") or (decision.get("reason") if status == "denied" else None),
            artifact=state.get("artifact"),
            arguments_hash=state.get("arguments_hash"),
        )
        return {"response": response.model_dump(mode="json")}

    graph = StateGraph(
        DirectToolState,
        context_schema=ExecutionContext,
        input_schema=DirectToolInput,
        output_schema=DirectToolOutput,
    )
    graph.add_node("validate_target", validate_target)
    graph.add_node("authorize", authorize)
    graph.add_node("approval", approval)
    graph.add_node(
        "execute_read",
        execute,
        retry_policy=RetryPolicy(
            initial_interval=0,
            backoff_factor=1,
            max_interval=0,
            max_attempts=read_max_attempts,
            jitter=False,
            retry_on=TransientToolError,
        ),
    )
    graph.add_node("execute_write", execute)
    graph.add_node("project_response", project_response)
    graph.add_edge(START, "validate_target")
    graph.add_edge("validate_target", "authorize")
    graph.add_conditional_edges("authorize", route_authorization)
    graph.add_conditional_edges("approval", route_approval)
    graph.add_edge("execute_read", "project_response")
    graph.add_edge("execute_write", "project_response")
    graph.add_edge("project_response", END)
    return graph.compile(checkpointer=checkpointer, name="direct_tool")
