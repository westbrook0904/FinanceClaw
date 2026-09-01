"""Foundation F5 FAST / PLAN / EXPLORE / Memory 真实调用 Gate。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from harness_bootstrap import build_harness
from harness_context import memory_context_item_id
from harness_contracts import (
    ExecutionMode,
    ExplorationBudget,
    ExplorationProfile,
    IdentityContext,
    InvocationContext,
    MemoryKind,
    MemorySensitivity,
    MemoryWriteDraft,
    PlanExecutionRecord,
    ProviderDescriptor,
    Request,
    RequestInput,
    RequestOptions,
    RequestTarget,
    ResultEnvelope,
    ResultStatus,
    TenantContext,
)
from harness_memory import SQLiteMemoryProvider
from harness_model import ModelGateway, OpenAIResponsesModelProvider
from harness_planning import LLMPlanner, PlanValidator
from harness_registry import InMemoryCapabilityRegistry, RegistryCapabilityCatalog
from harness_runtime import DefaultInvocationContextFactory, InvocationContextFactory
from harness_state import SQLiteStateStore
from harness_trace import InMemoryTracer, SpanType
from openai import AsyncOpenAI
from portfolio_risk_agent import PORTFOLIO_RISK_CAPABILITY_ID, PortfolioRiskAgentPlugin

_MODEL_CAPABILITY_ID = "model.finance-real-use/v1"
_MODEL_PROVIDER_ID = "openai:finance-real-use"
_MEMORY_NAMESPACE = "portfolio-risk"


class RealUseContextFactory(InvocationContextFactory):
    """示例认证边界；真实服务应从已认证会话解析这些值。"""

    def __init__(self, *, tenant_id: str, subject_id: str) -> None:
        self._tenant = TenantContext(tenant_id=tenant_id)
        self._identity = IdentityContext(subject=subject_id, scopes=frozenset({"portfolio:review"}))
        self._base = DefaultInvocationContextFactory()

    def create(self, request: Request) -> InvocationContext:
        base = self._base.create(request)
        return base.model_copy(update={"tenant": self._tenant, "identity": self._identity})


def default_portfolio_snapshot(*, include_limits: bool = True) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "portfolio_id": "f5-real-use-portfolio",
        "as_of": "2026-08-31T16:00:00+08:00",
        "base_currency": "CNY",
        "cash": "1000",
        "positions": [
            {
                "symbol": "AAA",
                "quantity": "10",
                "current_price": "100",
                "previous_close": "105",
            },
            {
                "symbol": "BBB",
                "quantity": "20",
                "current_price": "50",
                "previous_close": "49",
            },
        ],
    }
    if include_limits:
        snapshot["limits"] = _remembered_limits()
    return snapshot


async def run_live_gate(
    *,
    output_dir: Path,
    api_key: str,
    openai_model: str,
    base_url: str = "https://api.deepseek.com",
    reasoning_effort: str | None = "high",
    allow_insecure_http: bool = False,
    human_corrections: int = 0,
    client: AsyncOpenAI | None = None,
    live: bool = True,
) -> dict[str, object]:
    """执行一次真实 Gate 并落盘脱敏报告；调用者必须显式提供凭证和输出目录。"""

    if not isinstance(output_dir, Path):
        raise TypeError("output_dir must be Path")
    if not isinstance(human_corrections, int) or isinstance(human_corrections, bool):
        raise TypeError("human_corrections must be an integer")
    if human_corrections < 0:
        raise ValueError("human_corrections must not be negative")
    if not live and client is None:
        raise ValueError("non-live gate requires an explicit test SDK client")
    output_dir.mkdir(parents=True, exist_ok=True)
    evidence_paths = (
        output_dir / "execution-state.sqlite3",
        output_dir / "memory.sqlite3",
        output_dir / "real-use-report.json",
    )
    if any(path.exists() for path in evidence_paths):
        raise FileExistsError(
            "output_dir already contains real-use evidence; use a fresh directory"
        )
    run_id = uuid4().hex

    registry = InMemoryCapabilityRegistry()
    tracer = InMemoryTracer()
    provider = OpenAIResponsesModelProvider(
        api_key=api_key,
        openai_model=openai_model,
        model_capability_id=_MODEL_CAPABILITY_ID,
        provider_id=_MODEL_PROVIDER_ID,
        base_url=base_url,
        reasoning_effort=reasoning_effort,
        allow_insecure_http=allow_insecure_http,
        client=client,
    )
    registry.register_provider(
        provider,
        descriptor=ProviderDescriptor(
            provider_id=_MODEL_PROVIDER_ID,
            capability_id=_MODEL_CAPABILITY_ID,
            plugin_id="openai-responses",
            implementation_version="1.3.1",
            priority=100,
            tags=frozenset({"real-provider", "f5-gate"}),
            metadata={"api": "responses"},
        ),
    )
    catalog = RegistryCapabilityCatalog(registry)
    validator = PlanValidator(catalog, exploration_available=True)
    planner = LLMPlanner(
        ModelGateway(registry, tracer),
        planner_model_capability_id=_MODEL_CAPABILITY_ID,
        validator=validator,
        planner_id="f5-real-use-planner",
        allowed_capability_ids=(PORTFOLIO_RISK_CAPABILITY_ID,),
        max_output_tokens=2_048,
    )
    context_factory = RealUseContextFactory(
        tenant_id="financeclaw-real-use",
        subject_id="f5-reviewer",
    )
    state_store = SQLiteStateStore(output_dir / "execution-state.sqlite3")
    memory_provider = SQLiteMemoryProvider(output_dir / "memory.sqlite3")
    profile = ExplorationProfile(
        profile_id="f5-portfolio-explorer",
        model_capability_id=_MODEL_CAPABILITY_ID,
        allowed_capability_ids=frozenset({PORTFOLIO_RISK_CAPABILITY_ID}),
        default_budget=ExplorationBudget(
            max_steps=3,
            max_model_calls=4,
            max_action_calls=1,
            max_repeated_actions=0,
            max_observations=1,
        ),
        prompt_version="f5-portfolio-explore-v1",
        memory_required=True,
    )
    app = build_harness(
        registry=registry,
        tracer=tracer,
        capability_catalog=catalog,
        plan_validator=validator,
        plugins=(PortfolioRiskAgentPlugin(),),
        planners=(planner,),
        default_planner_id=planner.planner_id,
        exploration_profiles=(profile,),
        default_explorer_id=profile.profile_id,
        single_writer_guaranteed=True,
        context_factory=context_factory,
        memory_provider=memory_provider,
        memory_namespaces=(_MEMORY_NAMESPACE,),
        state_store=state_store,
        entry_point_group=None,
    )

    started_at = datetime.now(UTC)
    async with app:
        memory_record = await _write_preference(app, context_factory)
        fast_result = await app.handle(_fast_request())
        plan_result = await app.handle(_plan_request())
        explore_result = await app.handle(_explore_request())

    results = {
        "fast": fast_result,
        "plan": plan_result,
        "explore": explore_result,
    }
    records = {
        mode: await _load_record(state_store, result)
        for mode, result in results.items()
        if mode != "fast"
    }
    expected_memory_item_id = memory_context_item_id(memory_record)
    evidence = {
        mode: _run_evidence(
            mode,
            result,
            tracer=tracer,
            record=records.get(mode),
        )
        for mode, result in results.items()
    }
    explore_record = records.get("explore")
    memory_context_hit = _memory_context_hit(explore_record, expected_memory_item_id)
    memory_applied = _memory_applied(explore_record)
    repeated_action_count = _repeated_action_count(explore_record)
    error_counts = Counter(
        item["error_category"] for item in evidence.values() if item["error_category"] is not None
    )
    groundedness_passes = sum(bool(item["grounded"]) for item in evidence.values())
    checks_passed = (
        all(item["status"] == ResultStatus.SUCCESS.value for item in evidence.values())
        and groundedness_passes == len(evidence)
        and memory_context_hit
        and memory_applied
        and repeated_action_count == 0
        and human_corrections == 0
        and evidence["fast"]["model_span_count"] == 0
        and evidence["plan"]["model_span_count"] >= 1
        and evidence["explore"]["model_span_count"] >= 1
        and evidence["explore"]["action_span_count"] == 1
    )
    gate_passed = live and checks_passed
    report: dict[str, object] = {
        "schema_version": "financeclaw-real-use-gate-v1",
        "run_id": run_id,
        "live": live,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "provider": {
            "provider_id": _MODEL_PROVIDER_ID,
            "model": openai_model,
            "api": "responses",
            "reasoning_effort": reasoning_effort or "provider_default",
        },
        "scenario": {
            "id": "portfolio-risk-review",
            "capability_id": PORTFOLIO_RISK_CAPABILITY_ID,
            "portfolio_id": "f5-real-use-portfolio",
            "memory_namespace": _MEMORY_NAMESPACE,
        },
        "runs": evidence,
        "metrics": {
            "groundedness_passes": groundedness_passes,
            "groundedness_total": len(evidence),
            "memory_context_hit": memory_context_hit,
            "memory_applied": memory_applied,
            "repeated_action_count": repeated_action_count,
            "human_corrections": human_corrections,
            "error_counts": dict(sorted(error_counts.items())),
        },
        "checks_passed": checks_passed,
        "gate_passed": gate_passed,
    }
    report_path = output_dir / "real-use-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


async def _write_preference(app, context_factory: RealUseContextFactory):
    request = Request(
        request_id=f"memory-evidence-{uuid4().hex}",
        input=RequestInput(
            type="risk_preference",
            content={"limits": _remembered_limits()},
        ),
    )
    context = context_factory.create(request)
    gateway = app.memory_gateway
    if gateway is None:
        raise RuntimeError("real-use gate requires MemoryGateway")
    proposal = gateway.create_proposal(
        context,
        MemoryWriteDraft(
            kind=MemoryKind.PREFERENCE,
            content={"portfolio_risk_limits": _remembered_limits()},
            tags=frozenset({"portfolio", "risk-limit", "f5-real-use"}),
            evidence_refs=(f"request:{request.request_id}",),
        ),
        proposal_id=f"risk-preference-{request.request_id}",
        namespace=_MEMORY_NAMESPACE,
        sensitivity=MemorySensitivity.INTERNAL,
        producer="portfolio-risk-agent.real-use-gate",
    )
    return await gateway.put(context, proposal)


def _fast_request() -> Request:
    return Request(
        input=RequestInput(type="portfolio_snapshot", content=default_portfolio_snapshot()),
        target=RequestTarget(capability=PORTFOLIO_RISK_CAPABILITY_ID),
        options=RequestOptions(execution_mode=ExecutionMode.FAST, timeout_ms=120_000),
    )


def _plan_request() -> Request:
    return Request(
        input=RequestInput(type="portfolio_snapshot", content=default_portfolio_snapshot()),
        options=RequestOptions(execution_mode=ExecutionMode.PLAN, timeout_ms=120_000),
    )


def _explore_request() -> Request:
    return Request(
        input=RequestInput(
            type="portfolio_snapshot_with_remembered_limits",
            content=default_portfolio_snapshot(include_limits=False),
        ),
        options=RequestOptions(execution_mode=ExecutionMode.EXPLORE, timeout_ms=120_000),
    )


def _remembered_limits() -> dict[str, str]:
    return {"max_position_weight_pct": "30", "max_daily_loss_pct": "0.5"}


async def _load_record(
    state_store: SQLiteStateStore,
    result: ResultEnvelope,
) -> PlanExecutionRecord | None:
    plan_id = result.metadata.get("plan_id")
    return await state_store.load(plan_id) if isinstance(plan_id, str) else None


def _run_evidence(
    mode: str,
    result: ResultEnvelope,
    *,
    tracer: InMemoryTracer,
    record: PlanExecutionRecord | None,
) -> dict[str, object]:
    spans = tracer.spans(trace_id=result.trace_id) if result.trace_id is not None else ()
    outputs = _business_outputs(result, record)
    return {
        "mode": mode,
        "status": result.status.value,
        "trace_id": result.trace_id,
        "plan_id": result.metadata.get("plan_id"),
        "grounded": any(_grounded(output) for output in outputs),
        "business_output_count": len(outputs),
        "model_span_count": sum(span.type is SpanType.MODEL for span in spans),
        "action_span_count": sum(span.type is SpanType.ACTION for span in spans),
        "span_type_counts": dict(sorted(Counter(span.type.value for span in spans).items())),
        "error_category": result.error.category.value if result.error is not None else None,
        "error_code": result.error.code if result.error is not None else None,
    }


def _business_outputs(
    result: ResultEnvelope,
    record: PlanExecutionRecord | None,
) -> tuple[object, ...]:
    outputs: list[object] = []
    if result.output is not None and result.output.type == "portfolio_risk_review":
        outputs.append(result.output.data)
    if record is None:
        return tuple(outputs)
    for node in record.state.nodes.values():
        if (
            node.result is not None
            and node.result.output is not None
            and node.result.output.type == "portfolio_risk_review"
        ):
            outputs.append(node.result.output.data)
    for exploration in record.state.explorations.values():
        for observation in exploration.observations:
            summary = observation.bounded_summary
            if not isinstance(summary, Mapping):
                continue
            output = summary.get("output")
            if isinstance(output, Mapping) and output.get("type") == "portfolio_risk_review":
                outputs.append(output.get("data"))
    return tuple(outputs)


def _grounded(output: object) -> bool:
    if not isinstance(output, Mapping):
        return False
    valuation = output.get("valuation")
    grounding = output.get("grounding")
    breach_codes = [
        item.get("code") for item in output.get("breaches", []) if isinstance(item, Mapping)
    ]
    return (
        isinstance(valuation, Mapping)
        and valuation.get("net_asset_value") == "3000.00"
        and valuation.get("daily_pnl") == "-30.00"
        and isinstance(grounding, Mapping)
        and grounding.get("source") == "request_snapshot"
        and "DAILY_LOSS_LIMIT" in breach_codes
    )


def _memory_context_hit(record: PlanExecutionRecord | None, expected_item_id: str) -> bool:
    if record is None:
        return False
    return any(
        expected_item_id in use.included_item_ids
        for exploration in record.state.explorations.values()
        for use in exploration.context_uses
    )


def _memory_applied(record: PlanExecutionRecord | None) -> bool:
    if record is None:
        return False
    expected = _remembered_limits()
    return any(
        isinstance(action.proposal.input.content, Mapping)
        and action.proposal.input.content.get("limits") == expected
        for exploration in record.state.explorations.values()
        for action in exploration.actions
    )


def _repeated_action_count(record: PlanExecutionRecord | None) -> int:
    if record is None:
        return 0
    duplicates = 0
    for exploration in record.state.explorations.values():
        fingerprints = [
            json.dumps(
                {
                    "capability_id": action.proposal.capability_id,
                    "input": action.proposal.input.model_dump(mode="json"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for action in exploration.actions
        ]
        duplicates += len(fingerprints) - len(set(fingerprints))
    return duplicates


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the FinanceClaw F5 live real-use gate")
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly authorize real API calls and associated provider cost",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--openai-model", default=os.environ.get("OPENAI_MODEL"))
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        default=os.environ.get("OPENAI_REASONING_EFFORT", "high"),
        help="Responses reasoning effort; DeepSeek thinking mode normally uses high",
    )
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument("--human-corrections", type=int, default=0)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; the gate performs billable external model calls")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        parser.error("OPENAI_API_KEY is required")
    if not args.openai_model:
        parser.error("--openai-model or OPENAI_MODEL is required")
    report = asyncio.run(
        run_live_gate(
            output_dir=args.output_dir,
            api_key=api_key,
            openai_model=args.openai_model,
            base_url=args.base_url,
            reasoning_effort=args.reasoning_effort,
            allow_insecure_http=args.allow_insecure_http,
            human_corrections=args.human_corrections,
        )
    )
    print(json.dumps({"gate_passed": report["gate_passed"], "output_dir": str(args.output_dir)}))


if __name__ == "__main__":
    main()
