"""实现可审计、可审批且输出固定结构的投资组合复核工作流。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy, interrupt
from pydantic import BaseModel, ConfigDict, Field, model_validator

from financeclaw.kernel import ArtifactReference, ExecutionContext
from financeclaw.modules.artifacts import ArtifactService
from financeclaw.modules.audit import AuditEventType, AuditRecord, AuditRepository
from financeclaw.modules.workflows import (
    ApprovalPoint,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowTimeoutPolicy,
    WorkflowToolRef,
)
from financeclaw.orchestration.agents.middleware import (
    canonical_arguments_hash,
    trace_tool_authorization,
)
from financeclaw.orchestration.tools import (
    ToolCatalog,
    ToolDecisionType,
    ToolPolicy,
    TransientToolError,
)

WORKFLOW_ID = "portfolio_review"
WORKFLOW_VERSION = "1.0.0"
ASSISTANT_ID = "portfolio_review_v1"
APPROVAL_POINT = "publish_portfolio_report"
MARKET_TOOL_ID = "market_snapshot"
MARKET_TOOL_VERSION = "1.0.0"


class _FrozenModel(BaseModel):
    """定义不可变模型。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PortfolioPosition(_FrozenModel):
    """定义投资组合Position。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        symbol: 标准化金融标的代码。
        quantity: 持仓数量。
        cost_basis: 持仓单位成本或约定的成本基础。
    """

    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")
    quantity: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    cost_basis: Decimal = Field(ge=0, max_digits=24, decimal_places=8)


class PortfolioReviewInput(_FrozenModel):
    """定义投资组合Review的校验输入。

    适用场景：
        用于在数据进入领域或图运行前完成结构校验和类型收敛的场景。

    属性：
        portfolio_name: 用户可读的投资组合名称。
        positions: 待复核的持仓列表。
        max_snapshot_age_hours: 可接受的行情快照最大时效（小时）。
    """

    portfolio_name: str = Field(min_length=1, max_length=120)
    positions: tuple[PortfolioPosition, ...] = Field(min_length=1, max_length=20)
    max_snapshot_age_hours: int = Field(default=48, ge=1, le=168)

    @model_validator(mode="after")
    def symbols_must_be_unique(self) -> PortfolioReviewInput:
        """校验投资组合Review的校验输入的跨字段一致性；不满足不变量时拒绝构造。"""
        symbols = tuple(item.symbol.upper() for item in self.positions)
        if len(symbols) != len(set(symbols)):
            raise ValueError("portfolio positions must have unique symbols")
        return self


class PortfolioSourceReference(_FrozenModel):
    """定义投资组合来源的稳定引用。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        symbol: 标准化金融标的代码。
        provider: 产生行情或模型结果的供应方标识。
        as_of: 数据在供应方侧生效或采集的时间。
        input_hash: 原始输入的稳定哈希，用于证明数据来源未被替换。
        tool_version: 运行固定使用的版本，用于审计复现。
    """

    symbol: str
    provider: str
    as_of: datetime
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_version: str


class PortfolioReviewOutput(_FrozenModel):
    """定义投资组合Review的稳定输出。

    适用场景：
        用于把内部执行结果投影为稳定、可序列化的边界响应。

    属性：
        workflow_id: 工作流的稳定标识。
        workflow_version: 本次运行固定使用的工作流版本。
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        status: 当前生命周期状态，决定记录允许的后续操作。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定和篡改检测。
        portfolio_name: 用户可读的投资组合名称。
        snapshot_as_of: 本次分析所用行情中最早或统一的生效时间。
        total_market_value: 按行情计算的组合总市值，使用十进制字符串输出。
        largest_position_weight: 最大单一持仓占组合市值的比例。
        risk_band: 根据集中度等确定性规则得到的风险分档。
        source_refs: 支撑结果的行情来源、时间和版本引用。
        artifact: 详细报告的制品引用；未生成时为空。
        error: 失败原因的稳定文本；成功或未结束时为空。
    """

    workflow_id: Literal["portfolio_review"]
    workflow_version: Literal["1.0.0"]
    run_id: str
    status: Literal["completed", "rejected", "failed"]
    arguments_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    portfolio_name: str
    snapshot_as_of: datetime | None = None
    total_market_value: str | None = None
    largest_position_weight: str | None = None
    risk_band: Literal["low", "moderate", "high"] | None = None
    source_refs: tuple[PortfolioSourceReference, ...] = ()
    artifact: ArtifactReference | None = None
    error: str | None = None

    @model_validator(mode="after")
    def terminal_shape_matches_status(self) -> PortfolioReviewOutput:
        """校验投资组合Review的稳定输出的跨字段一致性；不满足不变量时拒绝构造。"""
        if self.status == "completed" and self.artifact is None:
            raise ValueError("completed portfolio review requires a report artifact")
        if self.status != "completed" and not self.error:
            raise ValueError("non-completed portfolio review requires an error")
        return self


class PortfolioReviewState(TypedDict, total=False):
    """定义投资组合Review的图运行状态。

    适用场景：
        用于图节点之间共享结构化字典，同时保留静态类型提示。

    属性：
        portfolio_name: 用户可读的投资组合名称。
        positions: 待复核的持仓列表。
        max_snapshot_age_hours: 可接受的行情快照最大时效（小时）。
        normalized_input: 经过工作流输入模型校验后的规范化数据。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定和篡改检测。
        snapshots: 按持仓顺序加载并通过来源校验的行情快照。
        snapshot_as_of: 本次分析所用行情中最早或统一的生效时间。
        analysis: 根据行情与持仓计算出的确定性分析指标。
        approval_id: 审批请求稳定标识。
        approval_outcome: 人工审批结果，用于决定继续、拒绝或按编辑后参数执行。
        artifact: 详细报告的制品引用；未生成时为空。
        error: 失败原因的稳定文本；成功或未结束时为空。
        workflow_id: 工作流的稳定标识。
        workflow_version: 本次运行固定使用的工作流版本。
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        status: 当前生命周期状态，决定记录允许的后续操作。
        total_market_value: 按行情计算的组合总市值，使用十进制字符串输出。
        largest_position_weight: 最大单一持仓占组合市值的比例。
        risk_band: 根据集中度等确定性规则得到的风险分档。
        source_refs: 支撑结果的行情来源、时间和版本引用。
    """

    portfolio_name: str
    positions: list[dict[str, Any]]
    max_snapshot_age_hours: int
    normalized_input: dict[str, Any]
    arguments_hash: str
    snapshots: list[dict[str, Any]]
    snapshot_as_of: str
    analysis: dict[str, str]
    approval_id: str
    approval_outcome: Literal["approved", "rejected", "invalid"]
    artifact: dict[str, Any]
    error: str
    workflow_id: str
    workflow_version: str
    run_id: str
    status: str
    total_market_value: str | None
    largest_position_weight: str | None
    risk_band: str | None
    source_refs: list[dict[str, Any]]


class PortfolioReviewProjection(TypedDict, total=False):
    """定义投资组合Review的公开投影。

    适用场景：
        用于图节点之间共享结构化字典，同时保留静态类型提示。

    属性：
        workflow_id: 工作流的稳定标识。
        workflow_version: 本次运行固定使用的工作流版本。
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        status: 当前生命周期状态，决定记录允许的后续操作。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定和篡改检测。
        portfolio_name: 用户可读的投资组合名称。
        snapshot_as_of: 本次分析所用行情中最早或统一的生效时间。
        total_market_value: 按行情计算的组合总市值，使用十进制字符串输出。
        largest_position_weight: 最大单一持仓占组合市值的比例。
        risk_band: 根据集中度等确定性规则得到的风险分档。
        source_refs: 支撑结果的行情来源、时间和版本引用。
        artifact: 详细报告的制品引用；未生成时为空。
        error: 失败原因的稳定文本；成功或未结束时为空。
    """

    workflow_id: str
    workflow_version: str
    run_id: str
    status: str
    arguments_hash: str
    portfolio_name: str
    snapshot_as_of: str | None
    total_market_value: str | None
    largest_position_weight: str | None
    risk_band: str | None
    source_refs: list[dict[str, Any]]
    artifact: dict[str, Any]
    error: str | None


def _parse_decision(value: Any) -> dict[str, Any]:
    """解析外部表示并转换为portfolio review v1 模块的数据。"""
    if not isinstance(value, Mapping):
        raise ValueError("workflow approval resume payload must be an object")
    decisions = value.get("decisions")
    if isinstance(decisions, list):
        if len(decisions) != 1 or not isinstance(decisions[0], Mapping):
            raise ValueError("workflow approval requires exactly one decision")
        return dict(decisions[0])
    return dict(value)


def _artifact_reference(metadata: Any) -> dict[str, Any]:
    """把制品服务元数据转换为工作流公开输出使用的引用字典。"""
    return {
        "artifact_id": metadata.artifact_id,
        "content_type": metadata.content_type,
        "content_hash": metadata.content_hash,
        "size_bytes": metadata.size_bytes,
    }


def build_portfolio_review_graph(
    *,
    catalog: ToolCatalog,
    policy: ToolPolicy,
    audit: AuditRepository,
    artifact_service: ArtifactService,
    checkpointer: Any = None,
    read_max_attempts: int = 3,
    clock: Callable[[], datetime] | None = None,
) -> Any:
    """根据已注入依赖组装portfolio review v1 模块的数据。"""
    now = clock or (lambda: datetime.now(UTC))
    managed_market = catalog.resolve(MARKET_TOOL_ID, MARKET_TOOL_VERSION)

    def append_tool_audit(
        context: ExecutionContext,
        *,
        event: AuditEventType,
        arguments_hash: str,
        decision: str,
        symbol: str,
    ) -> None:
        """为工作流内部工具授权或执行追加带运行关联的审计事件。"""
        audit.append(
            AuditRecord(
                event_type=event,
                tenant_id=context.tenant_id,
                subject_id=context.subject_id,
                conversation_id=None,
                turn_id=context.turn_id,
                run_id=context.run_id,
                resource_id=managed_market.governance.tool_id,
                resource_version=managed_market.governance.version,
                action="read",
                decision=decision,
                policy_version=policy.version,
                payload_hash=arguments_hash,
                metadata={
                    "workflow_id": WORKFLOW_ID,
                    "workflow_version": WORKFLOW_VERSION,
                    "symbol_hash": sha256(symbol.encode()).hexdigest()[:16],
                },
            )
        )

    def normalize(state: PortfolioReviewState) -> dict[str, Any]:
        """将输入规范化为可比较、可持久化的portfolio review v1 模块的数据。"""
        parsed = PortfolioReviewInput.model_validate(state)
        normalized = parsed.model_dump(mode="json")
        return {
            "normalized_input": normalized,
            "portfolio_name": parsed.portfolio_name,
            "positions": normalized["positions"],
            "max_snapshot_age_hours": parsed.max_snapshot_age_hours,
            "arguments_hash": canonical_arguments_hash(normalized),
            "error": "",
        }

    def load_snapshots(
        state: PortfolioReviewState, runtime: Runtime[ExecutionContext]
    ) -> dict[str, Any]:
        """从持久化表示加载并校验portfolio review v1 模块的数据。"""
        snapshots: list[dict[str, Any]] = []
        for position in state["positions"]:
            arguments = {"symbol": str(position["symbol"]).upper()}
            arguments = (
                managed_market.tool.get_input_schema()
                .model_validate(arguments)
                .model_dump(mode="json")
            )
            arguments_hash = canonical_arguments_hash(arguments)
            decision = policy.evaluate(runtime.context, managed_market.governance, arguments)
            trace_tool_authorization(
                tool_id=MARKET_TOOL_ID,
                effect=decision.effect.value,
                arguments_hash=arguments_hash,
                context_metadata=runtime.context.trace_metadata(),
            )
            if decision.effect is not ToolDecisionType.ALLOW:
                append_tool_audit(
                    runtime.context,
                    event=AuditEventType.TOOL_DENIED,
                    arguments_hash=arguments_hash,
                    decision=decision.effect.value,
                    symbol=arguments["symbol"],
                )
                raise PermissionError(decision.reason)
            append_tool_audit(
                runtime.context,
                event=AuditEventType.TOOL_ALLOWED,
                arguments_hash=arguments_hash,
                decision="allow",
                symbol=arguments["symbol"],
            )
            try:
                result = managed_market.tool.invoke(arguments)
                payload = json.loads(result) if isinstance(result, str) else result
                if not isinstance(payload, Mapping):
                    raise ValueError("market snapshot must be a JSON object")
                price = Decimal(str(payload["price"]))
                if price <= 0:
                    raise ValueError("market snapshot price must be positive")
                source = str(payload["provider"])
                as_of = datetime.fromisoformat(str(payload["as_of"]).replace("Z", "+00:00"))
                if as_of.tzinfo is None or as_of.utcoffset() is None:
                    raise ValueError("market snapshot as_of must include a timezone")
            except TransientToolError:
                append_tool_audit(
                    runtime.context,
                    event=AuditEventType.FINANCIAL_TOOL_FAILED,
                    arguments_hash=arguments_hash,
                    decision="transient_failure",
                    symbol=arguments["symbol"],
                )
                raise
            except Exception:
                append_tool_audit(
                    runtime.context,
                    event=AuditEventType.FINANCIAL_TOOL_FAILED,
                    arguments_hash=arguments_hash,
                    decision="invalid_result",
                    symbol=arguments["symbol"],
                )
                raise
            append_tool_audit(
                runtime.context,
                event=AuditEventType.FINANCIAL_TOOL_EXECUTED,
                arguments_hash=arguments_hash,
                decision="executed",
                symbol=arguments["symbol"],
            )
            snapshots.append(
                {
                    "symbol": arguments["symbol"],
                    "quantity": str(position["quantity"]),
                    "cost_basis": str(position["cost_basis"]),
                    "price": str(price),
                    "provider": source,
                    "as_of": as_of.isoformat(),
                    "input_hash": arguments_hash,
                    "tool_version": MARKET_TOOL_VERSION,
                }
            )
        return {"snapshots": snapshots}

    def validate_freshness(state: PortfolioReviewState) -> dict[str, Any]:
        """校验portfolio review v1 模块的数据的跨字段不变量并返回自身。"""
        as_of_values = [datetime.fromisoformat(item["as_of"]) for item in state["snapshots"]]
        oldest = min(as_of_values)
        current = now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise TypeError("workflow clock must return a timezone-aware datetime")
        maximum_age = timedelta(hours=state["max_snapshot_age_hours"])
        if oldest > current + timedelta(minutes=5):
            return {"error": "market snapshot as_of is unexpectedly in the future"}
        if current - oldest > maximum_age:
            return {"error": "market snapshot is older than the requested freshness limit"}
        return {"snapshot_as_of": oldest.isoformat(), "error": ""}

    def route_freshness(state: PortfolioReviewState) -> str:
        """根据行情时效校验结果选择进入分析节点或失败终结节点。"""
        return "finalize" if state.get("error") else "analyze_exposure"

    def analyze(state: PortfolioReviewState) -> dict[str, Any]:
        """使用 Decimal 计算组合市值、最大持仓权重和确定性风险分档。"""
        values = [Decimal(item["quantity"]) * Decimal(item["price"]) for item in state["snapshots"]]
        total = sum(values, Decimal("0"))
        largest_weight = max(values) / total
        if largest_weight >= Decimal("0.50"):
            risk_band = "high"
        elif largest_weight >= Decimal("0.30"):
            risk_band = "moderate"
        else:
            risk_band = "low"
        return {
            "analysis": {
                "total_market_value": format(total.quantize(Decimal("0.01")), "f"),
                "largest_position_weight": format(largest_weight.quantize(Decimal("0.0001")), "f"),
                "risk_band": risk_band,
            }
        }

    def request_approval(
        state: PortfolioReviewState, runtime: Runtime[ExecutionContext]
    ) -> dict[str, Any]:
        """生成与输入哈希绑定的审批中断，等待用户确认发布报告。"""
        approval_identity = {
            "run_id": runtime.context.run_id,
            "workflow_id": WORKFLOW_ID,
            "workflow_version": WORKFLOW_VERSION,
            "approval_point": APPROVAL_POINT,
            "arguments_hash": state["arguments_hash"],
        }
        approval_id = f"approval-{canonical_arguments_hash(approval_identity)[:24]}"
        decision = _parse_decision(
            interrupt(
                {
                    "approval_id": approval_id,
                    "approval_point": APPROVAL_POINT,
                    "workflow_id": WORKFLOW_ID,
                    "workflow_version": WORKFLOW_VERSION,
                    "requested_action": "publish_portfolio_report",
                    "arguments_hash": state["arguments_hash"],
                    "summary": state["analysis"],
                    "allowed_decisions": ["approve", "reject"],
                    "required_scope": "workflows:approve",
                }
            )
        )
        decision_type = decision.get("type")
        if decision_type == "reject":
            return {
                "approval_id": approval_id,
                "approval_outcome": "rejected",
                "error": str(decision.get("message") or "report publication rejected"),
            }
        if decision_type != "approve" or decision.get("arguments_hash") != state["arguments_hash"]:
            return {
                "approval_id": approval_id,
                "approval_outcome": "invalid",
                "error": "approval is missing or no longer matches workflow input",
            }
        return {"approval_id": approval_id, "approval_outcome": "approved", "error": ""}

    def route_approval(state: PortfolioReviewState) -> str:
        """根据审批决定选择继续执行、拒绝终结或重新校验参数。"""
        return "publish_report" if state.get("approval_outcome") == "approved" else "finalize"

    def publish_report(
        state: PortfolioReviewState, runtime: Runtime[ExecutionContext]
    ) -> dict[str, Any]:
        """发布portfolio review v1 模块的数据并返回供应方结果。"""
        report = {
            "schema_version": 1,
            "workflow_id": WORKFLOW_ID,
            "workflow_version": WORKFLOW_VERSION,
            "run_id": runtime.context.run_id,
            "portfolio_name": state["portfolio_name"],
            "arguments_hash": state["arguments_hash"],
            "analysis": state["analysis"],
            "snapshots": state["snapshots"],
            "disclaimer": "Point-in-time analytical report; not investment advice.",
        }
        metadata = artifact_service.persist(
            report,
            context=runtime.context,
            source_type="workflow_report",
            source_id=f"{WORKFLOW_ID}@{WORKFLOW_VERSION}",
            idempotency_key=f"{runtime.context.run_id}:{APPROVAL_POINT}:publish:v1",
        )
        return {"artifact": _artifact_reference(metadata)}

    def finalize(state: PortfolioReviewState, runtime: Runtime[ExecutionContext]) -> dict[str, Any]:
        """把图内部状态投影为满足终态 schema 的公开输出。"""
        analysis = state.get("analysis", {})
        outcome = state.get("approval_outcome")
        status = (
            "completed"
            if state.get("artifact")
            else "rejected"
            if outcome == "rejected"
            else "failed"
        )
        source_refs = [
            {
                "symbol": item["symbol"],
                "provider": item["provider"],
                "as_of": item["as_of"],
                "input_hash": item["input_hash"],
                "tool_version": item["tool_version"],
            }
            for item in state.get("snapshots", [])
        ]
        output = PortfolioReviewOutput(
            workflow_id=WORKFLOW_ID,
            workflow_version=WORKFLOW_VERSION,
            run_id=runtime.context.run_id,
            status=status,
            arguments_hash=state["arguments_hash"],
            portfolio_name=state["portfolio_name"],
            snapshot_as_of=state.get("snapshot_as_of"),
            total_market_value=analysis.get("total_market_value"),
            largest_position_weight=analysis.get("largest_position_weight"),
            risk_band=analysis.get("risk_band"),
            source_refs=source_refs,
            artifact=state.get("artifact"),
            error=state.get("error") or None,
        )
        return output.model_dump(mode="json")

    graph = StateGraph(
        PortfolioReviewState,
        context_schema=ExecutionContext,
        input_schema=PortfolioReviewInput,
        output_schema=PortfolioReviewProjection,
    )
    graph.add_node("normalize_input", normalize)
    graph.add_node(
        "load_market_snapshots",
        load_snapshots,
        retry_policy=RetryPolicy(
            initial_interval=0,
            backoff_factor=1,
            max_interval=0,
            max_attempts=read_max_attempts,
            jitter=False,
            retry_on=TransientToolError,
        ),
    )
    graph.add_node("validate_freshness", validate_freshness)
    graph.add_node("analyze_exposure", analyze)
    graph.add_node("publication_approval", request_approval)
    graph.add_node("publish_report", publish_report)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "normalize_input")
    graph.add_edge("normalize_input", "load_market_snapshots")
    graph.add_edge("load_market_snapshots", "validate_freshness")
    graph.add_conditional_edges("validate_freshness", route_freshness)
    graph.add_edge("analyze_exposure", "publication_approval")
    graph.add_conditional_edges("publication_approval", route_approval)
    graph.add_edge("publish_report", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer, name=ASSISTANT_ID)


def portfolio_review_definition(
    *,
    catalog: ToolCatalog,
    policy: ToolPolicy,
    audit: AuditRepository,
    artifact_service: ArtifactService,
    checkpointer: Any = None,
    read_max_attempts: int = 3,
    run_timeout_seconds: int = 300,
    approval_timeout_seconds: int = 900,
    clock: Callable[[], datetime] | None = None,
) -> WorkflowDefinition:
    """返回投资组合复核工作流的版本、权限、超时、schema 与图构建器。"""
    graph = build_portfolio_review_graph(
        catalog=catalog,
        policy=policy,
        audit=audit,
        artifact_service=artifact_service,
        checkpointer=checkpointer,
        read_max_attempts=read_max_attempts,
        clock=clock,
    )
    return WorkflowDefinition(
        workflow_id=WORKFLOW_ID,
        version=WORKFLOW_VERSION,
        assistant_id=ASSISTANT_ID,
        graph=graph,
        input_schema=PortfolioReviewInput,
        output_schema=PortfolioReviewOutput,
        model_profile_id="default@1.0.0",
        allowed_tools=(WorkflowToolRef(tool_id=MARKET_TOOL_ID, version=MARKET_TOOL_VERSION),),
        approval_points=(
            ApprovalPoint(
                approval_id=APPROVAL_POINT,
                description="Approve publishing the point-in-time portfolio review report.",
                requested_action="publish_portfolio_report",
            ),
        ),
        timeout_policy=WorkflowTimeoutPolicy(
            run_timeout_seconds=run_timeout_seconds,
            approval_timeout_seconds=approval_timeout_seconds,
        ),
        status=WorkflowStatus.ACTIVE,
        deployment_revision="portfolio-review-v1/revision-1",
        required_scopes=frozenset({"portfolio:review", "market:read"}),
    )
