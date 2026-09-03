"""直连工具调用图的装配实现。

位于 orchestration/graphs 图装配层，承载 ``/tool <id>`` 直连路径：
入参经工具 Pydantic Schema 校验归一化，按治理策略授权，需要时经
LangGraph interrupt 走人机审批，最后按读写副作用分流执行并投影响应。
"""

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
    """直连工具调用的输入 State：调用方提交的原始目标与参数。

    使用场景：作为 StateGraph 的 input_schema，仅接受这三个键，
    多余或缺失的字段由 validate_target 节点负责校验与反馈。

    Attributes:
        tool_id: 待调用工具的稳定标识，须在工具目录中可解析。
        version: 期望的工具版本；None 表示由目录解析最新可用版本。
        arguments: 工具入参字典，未经校验的原始形态，校验后会被规范化覆盖。

    """

    tool_id: str
    version: str | None
    arguments: dict[str, Any]


class DirectToolState(DirectToolInput, total=False):
    """直连工具图的全量运行 State：在输入之上累积校验、授权与执行结果。

    使用场景：作为 StateGraph 的 State 在节点间传递；所有键均可缺省
    （total=False），审批中断时即由 checkpoint 持久化这些键的取值。

    Attributes:
        tool_id: 待调用工具的稳定标识（继承自输入）。
        version: 期望的工具版本（继承自输入），可为 None。
        arguments: 工具入参，validate_target 后被规范化结果覆盖。
        resolved_version: validate_target 解析出的工具实际执行版本。
        normalized_arguments: 经工具输入 Schema 校验并 JSON 化的规范入参。
        arguments_hash: 规范入参的规范哈希，审计与审批比对共用。
        decision: 策略决策（ToolDecision 的 JSON 字典：effect/reason 等）。
        approval_id: 需审批时按上下文派生的确定性审批标识，否则为空串。
        approved_hash: 审批通过时登记的入参哈希；编辑或失效后清空。
        approval_outcome: 审批结论：approved/rejected/edited/invalidated。
        result: 工具执行结果；配置制品服务且超限时被替换为有界摘要。
        error: 当前错误信息，空串表示无错误。
        response: 终节点产出的 DirectToolResponse JSON 字典。
        artifact: 结果制品引用（artifact_id 等）；未产出制品时缺省。

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
    """直连工具图的输出 State：仅暴露最终响应，内部字段不外泄。

    使用场景：作为 StateGraph 的 output_schema，图运行结束只返回
    project_response 节点写入的响应字典。

    Attributes:
        response: DirectToolResponse 的 JSON 字典，含状态、结果与治理信息。

    """

    response: dict[str, Any]


def _parse_decision(value: Any) -> dict[str, Any]:
    """解析审批恢复负载，兼容 decisions 列表包装与扁平对象两种形态。

    Args:
        value: interrupt 恢复时得到的审批决定负载。

    Returns:
        收敛为单条审批决定的字典。

    Raises:
        ValueError: 负载不是对象，或 decisions 不是恰好一条决定时抛出。

    """
    if not isinstance(value, dict):
        raise ValueError("approval resume payload must be an object")
    decisions = value.get("decisions")
    if isinstance(decisions, list):
        if len(decisions) != 1 or not isinstance(decisions[0], dict):
            raise ValueError("approval resume requires exactly one decision")
        return dict(decisions[0])
    return dict(value)


def _approval_id(context: ExecutionContext, state: DirectToolState) -> str:
    """按（run_id，tool_id，版本，入参哈希）派生确定性审批标识。

    Args:
        context: 当前执行上下文，提供 run_id 等定位信息。
        state: 当前图 State，须已含 tool_id、resolved_version 与 arguments_hash。

    Returns:
        形如 ``approval-<24 位哈希>`` 的标识；同一调用重复派生不漂移。

    """
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
    """装配并编译直连工具调用图，返回名为 direct_tool 的可执行 graph。

    固定链路：START → validate_target → authorize →（授权分流：审批中断或
    读写执行）→ project_response → END；审批支持批准、改参重校验与驳回。

    Args:
        catalog: 工具目录，按（tool_id，version）解析出受治理的工具。
        policy: 工具治理策略，对每次调用评估 allow/deny/require_approval。
        audit: 审计仓储，写入工具调用全生命周期的审计事件。
        checkpointer: LangGraph checkpoint 后端，支撑审批中断后的恢复；可为 None。
        read_max_attempts: 只读执行遇瞬时错误的最大尝试次数，默认 3。
        artifact_service: 制品服务，超限结果 offload 为 Artifact；可为 None。

    Returns:
        编译后的 LangGraph 图（LangGraph CompiledGraph）。

    """

    def append_audit(
        context: ExecutionContext,
        state: DirectToolState,
        *,
        event: AuditEventType,
        decision: str,
        tool_call_id: str | None = None,
    ) -> None:
        """写入一条归属当前上下文的工具审计记录。

        Args:
            context: 当前执行上下文，提供租户、主体与运行定位。
            state: 当前图 State，提供工具定位、入参哈希与制品引用。
            event: 审计事件类型（allow/deny/approval/executed/failed 等）。
            decision: 决策或执行结论的描述性取值。
            tool_call_id: 本次执行的调用标识；缺省表示尚未执行。

        """
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
        """LangGraph 节点（validate_target）：校验直连目标并归一化入参。

        位于图的 START 之后第一个节点：按（tool_id，version）解析工具，
        要求治理配置允许直连（direct_invocation），再经工具输入 Schema
        完成 Pydantic 校验与 JSON 化规范化。

        Args:
            state: 当前图 State；读取 tool_id、version 与 arguments。

        Returns:
            写入 State 的增量：解析版本、规范化入参及其哈希，并清空
            error 与 approved_hash；失败时仅写 error，交由授权节点短路。

        """
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
        """LangGraph 节点（authorize）：评估工具治理策略并落授权审计。

        位于 validate_target 之后、所有执行路径之前：无错误时用策略评估
        当前调用，把决策 JSON 与派生的审批标识写入 State，并按决策落
        TOOL_ALLOWED/TOOL_DENIED/TOOL_APPROVAL_REQUESTED 审计事件。

        Args:
            state: 当前图 State；出错时直接放行短路，不做评估。
            runtime: LangGraph 运行时，提供 ExecutionContext。

        Returns:
            写入 State 的增量：decision 决策 JSON 与 approval_id。

        """
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
        """LangGraph 条件边路由（authorize 之后）：按授权决策分流。

        Args:
            state: 当前图 State，须已含 decision 决策 JSON。

        Returns:
            下一节点名：出错或拒绝去 project_response，需审批去 approval，
            其余按副作用分流到 execute_read/execute_write。

        """
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
        """LangGraph 节点（approval）：人机审批中断点。

        位于授权分流之后：经 LangGraph interrupt 挂起，携带审批标识、
        工具版本、规范化入参与哈希；恢复时复验审批决定——reject 记审计
        并置失败；edit 允许改参（改工具名会被拒），回退 validate_target
        重新校验；approve 须携带与挂起一致的入参哈希，否则判失效。

        Args:
            state: 当前图 State，须已含授权节点写入的审批上下文。
            runtime: LangGraph 运行时，提供 ExecutionContext。

        Returns:
            写入 State 的增量：approval_outcome，以及按结论附带的新参数、
            approved_hash 或 error。

        """
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
        """LangGraph 条件边路由（approval 之后）：按审批结论分流。

        Args:
            state: 当前图 State，须已含 approval_outcome。

        Returns:
            下一节点名：edited/invalidated 回 validate_target 重校验，
            rejected 或出错去 project_response，approved 按副作用执行。

        """
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
        """执行前二次授权：恢复/重放场景下重新评估策略后再放行。

        Args:
            state: 当前图 State，须已含 resolved_version 与规范化入参。
            context: 当前执行上下文，提供租户、主体与权限域。

        Returns:
            二元组（解析后的工具，本次策略决策）。

        Raises:
            PermissionError: 策略拒绝，或需审批但 approved_hash 与当前
                入参哈希不一致（审批通过后参数已被改动）时抛出。

        """
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
        """LangGraph 节点（execute_read/execute_write 复用）：执行工具。

        位于授权或审批通过之后：先做执行时二次授权，再以规范化入参
        调用工具；只读节点由 RetryPolicy 对瞬时错误重试，写节点不重试。
        失败记 FINANCIAL_TOOL_FAILED 后原样抛出；成功且配置制品服务时
        把超限结果 offload 为 Artifact，最后记 FINANCIAL_TOOL_EXECUTED。

        Args:
            state: 当前图 State，须已含二次授权所需字段。
            runtime: LangGraph 运行时，提供 ExecutionContext。

        Returns:
            写入 State 的增量：result、artifact（可为 None）、最新的
            decision JSON，并清空 error。

        """
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
        """LangGraph 终节点（project_response）：把 State 投影为响应体。

        位于所有执行与失败路径的汇合点：按结果与审批结论判定 status
        （success/rejected/denied/failed），版本缺省回退到入参版本或
        unknown，产出 DirectToolResponse 作为图的唯一输出。

        Args:
            state: 当前图 State，含执行结果或失败原因。
            runtime: LangGraph 运行时，提供 run_id。

        Returns:
            写入 State 的增量：response 响应 JSON 字典。

        """
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

    # 直连工具图：输入输出契约收敛，运行上下文为 ExecutionContext。
    graph = StateGraph(
        DirectToolState,
        context_schema=ExecutionContext,
        input_schema=DirectToolInput,
        output_schema=DirectToolOutput,
    )
    # 节点注册：目标校验、授权、审批中断、读写执行与响应投影。
    graph.add_node("validate_target", validate_target)
    graph.add_node("authorize", authorize)
    graph.add_node("approval", approval)
    # 只读执行节点：瞬时错误按固定节奏重试至多 read_max_attempts 次。
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
    # 写执行节点：副作用不可安全重放，不配置重试。
    graph.add_node("execute_write", execute)
    graph.add_node("project_response", project_response)
    # 固定主干边与两处条件分流（授权后按决策、审批后按结论）。
    graph.add_edge(START, "validate_target")
    graph.add_edge("validate_target", "authorize")
    graph.add_conditional_edges("authorize", route_authorization)
    graph.add_conditional_edges("approval", route_approval)
    graph.add_edge("execute_read", "project_response")
    graph.add_edge("execute_write", "project_response")
    graph.add_edge("project_response", END)
    # 以 checkpointer 支撑审批中断后的恢复，图名 direct_tool。
    return graph.compile(checkpointer=checkpointer, name="direct_tool")
