"""构建带策略判断、审批中断、重试和制品投影的直接工具图。"""

from typing import Any, Literal, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy, interrupt

from financeclaw.kernel import DirectToolResponse, ExecutionContext
from financeclaw.modules.artifacts import ArtifactService
from financeclaw.modules.audit import AuditEventType, AuditRecord, AuditRepository
from financeclaw.orchestration.agents.middleware import (
    canonical_arguments_hash,
    trace_tool_authorization,
)
from financeclaw.orchestration.tools import (
    SideEffect,
    ToolCatalog,
    ToolDecisionType,
    ToolPolicy,
    TransientToolError,
)


class DirectToolInput(TypedDict):
    """定义直接工具的校验输入。

    适用场景：
        用于图节点之间共享结构化字典，同时保留静态类型提示。

    属性：
        tool_id: 工具的稳定标识。
        version: 语义版本，用于固定运行行为并支持审计复现。
        arguments: 传给目标工具或工作流的已解析参数。
    """

    tool_id: str
    version: str | None
    arguments: dict[str, Any]


class DirectToolState(DirectToolInput, total=False):
    """定义直接工具的图运行状态。

    适用场景：
        用于 LangGraph 节点之间共享逐步填充的运行状态。

    属性：
        resolved_version: 运行固定使用的版本，用于审计复现。
        normalized_arguments: 经过入参模型校验和规范化后的工具参数。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定和篡改检测。
        decision: 审批人或策略引擎作出的结构化决定。
        approval_id: 审批请求稳定标识。
        approved_hash: 审批时确认的参数哈希，防止恢复时参数被替换。
        approval_outcome: 人工审批结果，用于决定继续、拒绝或按编辑后参数执行。
        result: 内部步骤产生、等待后续投影的执行结果。
        error: 失败原因的稳定文本；成功或未结束时为空。
        response: 投影到公开边界后的结构化响应。
        artifact: 详细报告的制品引用；未生成时为空。
    """

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
    """定义直接工具的稳定输出。

    适用场景：
        用于图节点之间共享结构化字典，同时保留静态类型提示。

    属性：
        response: 投影到公开边界后的结构化响应。
    """

    response: dict[str, Any]


def _parse_decision(value: Any) -> dict[str, Any]:
    """解析外部表示并转换为direct tool 模块的数据。"""
    if not isinstance(value, dict):
        raise ValueError("approval resume payload must be an object")
    decisions = value.get("decisions")
    if isinstance(decisions, list):
        if len(decisions) != 1 or not isinstance(decisions[0], dict):
            raise ValueError("approval resume requires exactly one decision")
        return dict(decisions[0])
    return dict(value)


def _approval_id(context: ExecutionContext, state: DirectToolState) -> str:
    """由运行、目标和参数哈希生成稳定审批标识，确保恢复绑定原请求。"""
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
    """根据已注入依赖组装direct tool 模块的数据。"""

    def append_audit(
        context: ExecutionContext,
        state: DirectToolState,
        *,
        event: AuditEventType,
        decision: str,
        tool_call_id: str | None = None,
    ) -> None:
        """为直接工具图的授权、审批和执行阶段追加不可变审计事件。"""
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
        """校验direct tool 模块的数据的跨字段不变量并返回自身。"""
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
        """解析工具版本、校验参数并执行首次策略授权。"""
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
        """依据策略决策选择拒绝、请求审批或直接执行。"""
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
        """创建与规范化参数哈希绑定的 LangGraph 人工审批中断。"""
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
        """根据审批决定选择继续执行、拒绝终结或重新校验参数。"""
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
        """在审批恢复后对最终参数重新运行策略，防止编辑绕过治理。"""
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
        """按重试策略调用工具，并把成功结果或最终错误写入图状态。"""
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
        """将直接工具图内部状态收敛为稳定响应，并按阈值外置大结果。"""
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
