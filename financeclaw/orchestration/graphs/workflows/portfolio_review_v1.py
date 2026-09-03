"""首个固定流程 portfolio_review@1.0.0 的图与发布定义装配实现。

位于 orchestration/graphs/workflows 固定流程子包，按“校验输入 → 读取
带来源与时间戳的行情 → 校验新鲜度 → 确定性计算集中度 → interrupt 审批
→ 发布报告制品”的固定链路编排；State/checkpoint 只保留有界结构化数据
和制品引用。
"""

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

# 工作流稳定标识，与版本共同构成工作流目录的索引键。
WORKFLOW_ID = "portfolio_review"
# 工作流语义化版本号，形如 x.y.z。
WORKFLOW_VERSION = "1.0.0"
# Agent Server 侧承载该流程的助手标识，同时是编译后的图名。
ASSISTANT_ID = "portfolio_review_v1"
# 唯一审批检查点：发布组合评审报告前的人工确认。
APPROVAL_POINT = "publish_portfolio_report"
# 流程固定使用的行情快照工具及其版本（不允许运行期漂移）。
MARKET_TOOL_ID = "market_snapshot"
MARKET_TOOL_VERSION = "1.0.0"


class _FrozenModel(BaseModel):
    """流程契约模型的内部基类：冻结、禁止多余字段且拒绝 NaN/Inf。

    使用场景：本模块全部 Pydantic 契约模型统一继承，保证输入输出
    不可变、结构严格，杜绝隐式字段注入与非法数值。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PortfolioPosition(_FrozenModel):
    """单个持仓的输入契约：证券代码、数量与成本。

    使用场景：作为 ``PortfolioReviewInput.positions`` 的元素，经 Pydantic
    严格校验后参与市值与集中度的确定性计算。

    Attributes:
        symbol: 证券代码，1 到 16 字符，仅允许字母、数字与 ``.`` ``_`` ``-``。
        quantity: 持仓数量，正数，最多 24 位数字、8 位小数。
        cost_basis: 持仓成本，非负数，精度约束与数量一致。

    """

    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z0-9._-]+$")
    quantity: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    cost_basis: Decimal = Field(ge=0, max_digits=24, decimal_places=8)


class PortfolioReviewInput(_FrozenModel):
    """portfolio_review@1.0.0 的输入契约。

    使用场景：作为图的 input_schema，在 normalize_input 节点完成校验
    与 JSON 化归一化，并派生后续审批与审计共用的入参哈希。

    Attributes:
        portfolio_name: 组合名称，1 到 120 字符。
        positions: 持仓列表，1 到 20 条，证券代码大小写不敏感地唯一。
        max_snapshot_age_hours: 允许的行情快照最长大龄（小时），默认 48，取值 1 到 168。

    """

    portfolio_name: str = Field(min_length=1, max_length=120)
    positions: tuple[PortfolioPosition, ...] = Field(min_length=1, max_length=20)
    max_snapshot_age_hours: int = Field(default=48, ge=1, le=168)

    @model_validator(mode="after")
    def symbols_must_be_unique(self) -> PortfolioReviewInput:
        """校验持仓的证券代码大小写不敏感地不重复。

        Raises:
            ValueError: 存在重复证券代码时抛出。

        """
        symbols = tuple(item.symbol.upper() for item in self.positions)
        if len(symbols) != len(set(symbols)):
            raise ValueError("portfolio positions must have unique symbols")
        return self


class PortfolioSourceReference(_FrozenModel):
    """单条行情来源引用：记录产出计算输入的快照溯源信息。

    使用场景：finalize 阶段由快照投影而来，随输出返回，便于审计与
    复核每笔行情的提供方、时点与版本。

    Attributes:
        symbol: 该快照对应的证券代码（已归一为大写）。
        provider: 行情提供方标识。
        as_of: 快照时点（ISO 格式，带时区）。
        input_hash: 该次行情调用的规范入参哈希（64 位小写十六进制）。
        tool_version: 产出快照的工具版本。

    """

    symbol: str
    provider: str
    as_of: datetime
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_version: str


class PortfolioReviewOutput(_FrozenModel):
    """portfolio_review@1.0.0 的输出契约（终态结果）。

    使用场景：finalize 节点构造并返回，经 output_schema 校验后成为图
    输出；校验器保证终态自洽——completed 必带制品，非 completed 必带
    错误信息。

    Attributes:
        workflow_id: 工作流标识，固定为 portfolio_review。
        workflow_version: 工作流版本，固定为 1.0.0。
        run_id: 本次运行的唯一 ID。
        status: 终态：completed（已发布报告）、rejected（审批驳回）或 failed。
        arguments_hash: 归一化输入的规范哈希，串联审计与审批比对。
        portfolio_name: 组合名称（回显输入）。
        snapshot_as_of: 全组合最旧快照时点；未通过新鲜度校验时为 None。
        total_market_value: 组合总市值（两位小数字符串）；未完成分析时为 None。
        largest_position_weight: 最大单一持仓权重（四位小数字符串）；未完成分析时为 None。
        risk_band: 集中度风险档（low/moderate/high）；未完成分析时为 None。
        source_refs: 行情来源引用列表；无可用快照时为空元组。
        artifact: 已发布报告的制品引用；仅 completed 时非 None。
        error: 失败或驳回原因；completed 时为 None。

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
        """校验终态自洽：completed 必带制品，非 completed 必带错误信息。

        Raises:
            ValueError: 终态与制品、错误信息不匹配时抛出。

        """
        if self.status == "completed" and self.artifact is None:
            raise ValueError("completed portfolio review requires a report artifact")
        if self.status != "completed" and not self.error:
            raise ValueError("non-completed portfolio review requires an error")
        return self


class PortfolioReviewState(TypedDict, total=False):
    """流程运行期 State：在输入之上累积快照、分析、审批与产出引用。

    使用场景：作为 StateGraph 的 State 在节点间传递；所有键均可缺省
    （total=False），审批中断时 checkpoint 持久化的即这些键的取值。

    Attributes:
        portfolio_name: 组合名称（输入回显）。
        positions: 归一化后的持仓列表，每项含 symbol/quantity/cost_basis。
        max_snapshot_age_hours: 允许的行情快照最长大龄（小时）。
        normalized_input: 经 Pydantic 校验并 JSON 化的完整输入。
        arguments_hash: normalized_input 的规范哈希，审批与审计共用。
        snapshots: 各持仓的行情快照（价格、提供方、时点、入参哈希等）。
        snapshot_as_of: 全组合最旧快照时点的 ISO 字符串。
        analysis: 确定性分析结果：总市值、最大权重与风险档。
        approval_id: 发布审批点派生的确定性审批标识。
        approval_outcome: 审批结论：approved/rejected/invalid。
        artifact: 已发布报告的制品引用；未发布时缺省。
        error: 当前错误信息，空串表示无错误。
        workflow_id: 工作流标识回填（终态输出用）。
        workflow_version: 工作流版本回填（终态输出用）。
        run_id: 本次运行 ID 回填（终态输出用）。
        status: 终态状态回填：completed/rejected/failed。
        total_market_value: 组合总市值（两位小数字符串）；未分析时缺省。
        largest_position_weight: 最大持仓权重（四位小数字符串）；未分析时缺省。
        risk_band: 集中度风险档；未分析时缺省。
        source_refs: 行情来源引用列表，由 snapshots 投影而来。

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
    """流程对外输出投影：与运行期 State 解耦的 output_schema。

    使用场景：作为 StateGraph 的 output_schema，finalize 返回的字典经
    其键过滤后成为图输出，内部中间字段不外泄。

    Attributes:
        workflow_id: 工作流标识（portfolio_review）。
        workflow_version: 工作流版本（1.0.0）。
        run_id: 本次运行的唯一 ID。
        status: 终态：completed/rejected/failed。
        arguments_hash: 归一化输入的规范哈希。
        portfolio_name: 组合名称。
        snapshot_as_of: 全组合最旧快照时点；未通过新鲜度校验时为 None。
        total_market_value: 组合总市值字符串；未完成分析时为 None。
        largest_position_weight: 最大持仓权重字符串；未完成分析时为 None。
        risk_band: 集中度风险档；未完成分析时为 None。
        source_refs: 行情来源引用列表。
        artifact: 报告制品引用；仅 completed 时非 None。
        error: 失败或驳回原因；completed 时为 None。

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
    """解析审批恢复负载，兼容 decisions 列表包装与扁平对象两种形态。

    Args:
        value: interrupt 恢复时得到的审批决定负载。

    Returns:
        收敛为单条审批决定的字典。

    Raises:
        ValueError: 负载不是对象，或 decisions 不是恰好一条决定时抛出。

    """
    if not isinstance(value, Mapping):
        raise ValueError("workflow approval resume payload must be an object")
    decisions = value.get("decisions")
    if isinstance(decisions, list):
        if len(decisions) != 1 or not isinstance(decisions[0], Mapping):
            raise ValueError("workflow approval requires exactly one decision")
        return dict(decisions[0])
    return dict(value)


def _artifact_reference(metadata: Any) -> dict[str, Any]:
    """把制品元数据收敛为有界引用字典，供 State 与输出携带。

    Args:
        metadata: 制品服务返回的 ArtifactMetadata。

    Returns:
        仅含 artifact_id、content_type、content_hash 与 size_bytes 的字典。

    """
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
    """装配并编译 portfolio_review@1.0.0 的固定流程图。

    固定链路：START → normalize_input → load_market_snapshots →
    validate_freshness →（失败走 finalize）→ analyze_exposure →
    publication_approval →（approved 走 publish_report）→ finalize → END。

    Args:
        catalog: 工具目录，用于解析 market_snapshot@1.0.0。
        policy: 工具治理策略，对每次行情读取做授权评估。
        audit: 审计仓储，记录行情读取的授权与执行审计。
        artifact_service: 制品服务，报告经幂等键持久化为制品。
        checkpointer: LangGraph checkpoint 后端，支撑审批中断恢复；可为 None。
        read_max_attempts: 行情读取遇瞬时错误的最大尝试次数，默认 3。
        clock: 注入时钟，返回当前时间；缺省为系统 UTC 时间（测试可注入）。

    Returns:
        编译后的 LangGraph 图，图名为 portfolio_review_v1。

    """
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
        """写入一条行情读取相关的审计记录。

        Args:
            context: 当前执行上下文，提供租户、主体与运行定位。
            event: 审计事件类型（allow/denied/failed/executed 等）。
            arguments_hash: 本次行情调用的规范入参哈希。
            decision: 策略或执行结论的描述性取值。
            symbol: 原始证券代码；落审计前经哈希截断脱敏。

        """
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
        """LangGraph 节点（normalize_input）：校验输入并归一化。

        位于图的 START 之后第一个节点：用 PortfolioReviewInput 完成校验
        与 JSON 化，回显关键字段并派生 arguments_hash，供后续授权、审批
        与制品幂等共用。

        Args:
            state: 当前图 State，直接承载原始输入字段。

        Returns:
            写入 State 的增量：normalized_input、portfolio_name、positions、
            max_snapshot_age_hours、arguments_hash，并清空 error。

        """
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
        """LangGraph 节点（load_market_snapshots）：逐持仓读取行情快照。

        位于 normalize_input 之后：对每个证券代码，先归一化入参并评估
        授权策略（非 allow 记 denied 审计后直接拒绝），再调用行情工具；
        返回值必须是 JSON 对象且价格正数、时点带时区。瞬时错误由节点
        上的 RetryPolicy 重试，其余失败分类落审计后原样抛出。

        Args:
            state: 当前图 State，读取归一化后的 positions。
            runtime: LangGraph 运行时，提供 ExecutionContext。

        Returns:
            写入 State 的增量：snapshots 快照列表（带来源与时间戳）。

        """
        snapshots: list[dict[str, Any]] = []
        for position in state["positions"]:
            # 1. 归一化行情入参：代码转大写并经工具输入 Schema 校验，派生参数哈希。
            arguments = {"symbol": str(position["symbol"]).upper()}
            arguments = (
                managed_market.tool.get_input_schema()
                .model_validate(arguments)
                .model_dump(mode="json")
            )
            arguments_hash = canonical_arguments_hash(arguments)
            # 2. 评估工具授权策略并落审计；非 allow 记 denied 审计后直接拒绝。
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
            # 3. 调用行情工具并校验返回：必须是 JSON 对象，价格正数、时点带时区。
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
            # 4. 失败分类落审计后原样抛出：瞬时错误交由节点重试，其余立即失败。
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
            # 5. 成功即落执行审计，并把带来源与时间戳的快照写入 State。
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
        """LangGraph 节点（validate_freshness）：检查行情新鲜度。

        位于 load_market_snapshots 之后：取全部快照中最旧的 as_of 作为
        基准，拒绝来自未来的快照，并按 max_snapshot_age_hours 拒绝过期
        行情。通过时回填 snapshot_as_of 并清空 error；失败仅写 error，
        交由条件边转入 finalize 以 failed 终态收束。

        Args:
            state: 当前图 State，读取 snapshots 与 max_snapshot_age_hours。

        Returns:
            写入 State 的增量：snapshot_as_of 并清空 error；失败时仅写 error。

        Raises:
            TypeError: 注入时钟返回未带时区的时间时抛出。

        """
        # 1. 取最旧快照时点，作为全组合统一的新鲜度基准。
        as_of_values = [datetime.fromisoformat(item["as_of"]) for item in state["snapshots"]]
        oldest = min(as_of_values)
        current = now()
        # 2. 注入时钟必须返回带时区的时间，防止跨时区比较失真。
        if current.tzinfo is None or current.utcoffset() is None:
            raise TypeError("workflow clock must return a timezone-aware datetime")
        maximum_age = timedelta(hours=state["max_snapshot_age_hours"])
        # 3. 拒绝来自未来的快照（容忍 5 分钟时钟偏差），再拒绝超龄行情。
        if oldest > current + timedelta(minutes=5):
            return {"error": "market snapshot as_of is unexpectedly in the future"}
        if current - oldest > maximum_age:
            return {"error": "market snapshot is older than the requested freshness limit"}
        return {"snapshot_as_of": oldest.isoformat(), "error": ""}

    def route_freshness(state: PortfolioReviewState) -> str:
        """LangGraph 条件边路由（validate_freshness 之后）。

        Args:
            state: 当前图 State，以 error 是否为空判定新鲜度结论。

        Returns:
            下一节点名：通过去 analyze_exposure，失败去 finalize。

        """
        return "finalize" if state.get("error") else "analyze_exposure"

    def analyze(state: PortfolioReviewState) -> dict[str, Any]:
        """LangGraph 节点（analyze_exposure）：确定性计算组合集中度。

        位于新鲜度校验之后：用 Decimal 精确计算各持仓市值、总市值与
        最大单一持仓权重，并按 50%/30% 阈值划分风险档，结果写入
        State.analysis；纯函数计算，不触发任何外部调用。

        Args:
            state: 当前图 State，读取 snapshots 快照。

        Returns:
            写入 State 的增量：analysis 分析结果字典。

        """
        # 1. 计算各持仓市值与组合总市值。
        values = [Decimal(item["quantity"]) * Decimal(item["price"]) for item in state["snapshots"]]
        total = sum(values, Decimal("0"))
        # 2. 计算最大单一持仓权重，并按 50%/30% 阈值划分风险档。
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
        """LangGraph 节点（publication_approval）：发布前人工审批中断点。

        位于 analyze_exposure 之后：按（run_id，工作流标识与版本，审批
        点，入参哈希）派生确定性审批标识，经 LangGraph interrupt 挂起，
        携带分析摘要与所需权限域；恢复时复验审批决定——reject 置驳回，
        approve 必须携带与挂起一致的入参哈希，缺失或不一致判 invalid。

        Args:
            state: 当前图 State，须已含 arguments_hash 与 analysis。
            runtime: LangGraph 运行时，提供 ExecutionContext。

        Returns:
            写入 State 的增量：approval_id、approval_outcome 与 error。

        """
        # 1. 派生确定性审批标识：同一 run、同一输入重复恢复不会漂移。
        approval_identity = {
            "run_id": runtime.context.run_id,
            "workflow_id": WORKFLOW_ID,
            "workflow_version": WORKFLOW_VERSION,
            "approval_point": APPROVAL_POINT,
            "arguments_hash": state["arguments_hash"],
        }
        approval_id = f"approval-{canonical_arguments_hash(approval_identity)[:24]}"
        # 2. interrupt 挂起并携带审批上下文（含分析摘要与所需权限域）。
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
        # 3. 解析恢复负载：reject 置驳回；approve 复验入参哈希一致性。
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
        """LangGraph 条件边路由（publication_approval 之后）。

        Args:
            state: 当前图 State，读取 approval_outcome 审批结论。

        Returns:
            下一节点名：仅 approved 去 publish_report，其余去 finalize。

        """
        return "publish_report" if state.get("approval_outcome") == "approved" else "finalize"

    def publish_report(
        state: PortfolioReviewState, runtime: Runtime[ExecutionContext]
    ) -> dict[str, Any]:
        """LangGraph 节点（publish_report）：发布报告制品。

        位于审批通过之后：组装带 schema 版本、运行定位、分析结果与快照
        溯源的报告，并以 run/审批点级幂等键持久化到制品服务——审批恢复
        重放时命中同一制品，State 只写入有界的制品引用。

        Args:
            state: 当前图 State，读取分析结果与快照。
            runtime: LangGraph 运行时，提供 run_id 等运行定位。

        Returns:
            写入 State 的增量：artifact 制品引用字典。

        """
        # 1. 组装报告：含 schema 版本、运行定位、分析结果与快照溯源。
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
        # 2. 以 run+审批点级幂等键持久化，重放恢复时天然命中同一制品。
        metadata = artifact_service.persist(
            report,
            context=runtime.context,
            source_type="workflow_report",
            source_id=f"{WORKFLOW_ID}@{WORKFLOW_VERSION}",
            idempotency_key=f"{runtime.context.run_id}:{APPROVAL_POINT}:publish:v1",
        )
        return {"artifact": _artifact_reference(metadata)}

    def finalize(state: PortfolioReviewState, runtime: Runtime[ExecutionContext]) -> dict[str, Any]:
        """LangGraph 终节点（finalize）：产出终态投影并输出。

        位于 publish_report 与失败路径的汇合点：按制品与审批结论判定
        status，把快照收敛为来源引用，并经 PortfolioReviewOutput 校验
        终态自洽后返回 JSON 字典，成为图的最终输出。

        Args:
            state: 当前图 State，含分析结果、审批结论或错误信息。
            runtime: LangGraph 运行时，提供 run_id。

        Returns:
            PortfolioReviewOutput 的 JSON 字典，经 output_schema 过滤后输出。

        """
        # 1. 判定终态：有制品即 completed，审批驳回为 rejected，其余为 failed。
        analysis = state.get("analysis", {})
        outcome = state.get("approval_outcome")
        status = (
            "completed"
            if state.get("artifact")
            else "rejected"
            if outcome == "rejected"
            else "failed"
        )
        # 2. 把快照收敛为来源引用，保留提供方、时点与入参哈希。
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
        # 3. 经输出契约校验（completed 必带制品、非 completed 必带错误）后返回。
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

    # 固定流程图：输入输出契约收敛，运行上下文为 ExecutionContext。
    graph = StateGraph(
        PortfolioReviewState,
        context_schema=ExecutionContext,
        input_schema=PortfolioReviewInput,
        output_schema=PortfolioReviewProjection,
    )
    # 节点注册：输入归一化、行情读取、新鲜度校验、集中度分析、审批、发布与终态投影。
    graph.add_node("normalize_input", normalize)
    # 行情读取节点：瞬时错误按固定节奏重试至多 read_max_attempts 次。
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
    # 固定主干边；两处条件分流分别处理新鲜度失败与审批结论。
    graph.add_edge(START, "normalize_input")
    graph.add_edge("normalize_input", "load_market_snapshots")
    graph.add_edge("load_market_snapshots", "validate_freshness")
    graph.add_conditional_edges("validate_freshness", route_freshness)
    graph.add_edge("analyze_exposure", "publication_approval")
    graph.add_conditional_edges("publication_approval", route_approval)
    graph.add_edge("publish_report", "finalize")
    graph.add_edge("finalize", END)
    # 以 checkpointer 支撑审批中断后的恢复，图名 portfolio_review_v1。
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
    """把流程图封装为可注册进工作流目录的发布定义。

    使用场景：启动装配期由工作流目录登记；固化允许的工具版本、审批
    点、超时策略、权限域与部署修订号，运行期按（workflow_id，version）
    取用并启动运行。

    Args:
        catalog: 工具目录，透传给 build_portfolio_review_graph。
        policy: 工具治理策略，透传给 build_portfolio_review_graph。
        audit: 审计仓储，透传给 build_portfolio_review_graph。
        artifact_service: 制品服务，透传给 build_portfolio_review_graph。
        checkpointer: checkpoint 后端，透传给 build_portfolio_review_graph。
        read_max_attempts: 行情读取最大尝试次数，透传给 build_portfolio_review_graph。
        run_timeout_seconds: 单次运行超时（秒），默认 300。
        approval_timeout_seconds: 审批等待超时（秒），默认 900。
        clock: 注入时钟，透传给 build_portfolio_review_graph。

    Returns:
        状态为 ACTIVE 的 WorkflowDefinition，含输入输出契约与权限域要求。

    """
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
