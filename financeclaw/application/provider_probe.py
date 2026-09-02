"""Credential-gated Stage-1 Provider and LangSmith acceptance probe."""

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass

from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from pydantic import BaseModel, Field

from financeclaw.audit import AuditEventType, InMemoryAuditRepository
from financeclaw.bootstrap import build_components
from financeclaw.contracts import ExecutionContext
from financeclaw.infrastructure import FinanceClawSettings


class QuoteIntent(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    should_read: bool


@dataclass(frozen=True, slots=True)
class Stage1ProviderProbeResult:
    model_type: str
    tool_calling: bool
    structured_output: dict[str, object]
    governed_tool_executed: bool
    trace_url: str | None


@traceable(name="stage1.provider.probe", run_type="chain", tags=["stage:1"])
async def probe_provider(settings: FinanceClawSettings) -> Stage1ProviderProbeResult:
    """Exercise the configured OpenAI-compatible model and governed Agent path."""

    audit = InMemoryAuditRepository()
    components = build_components(settings, audit=audit)
    profile = components.default_agent_profile
    model = components.model_factory.create(profile.model_profile)
    market_tool = components.tool_catalog.resolve("market_snapshot").tool

    tool_response = await model.bind_tools([market_tool]).ainvoke(
        "Call market_snapshot for AAPL. Do not answer without making that tool call."
    )
    structured_response = await model.with_structured_output(
        QuoteIntent, method="json_mode"
    ).ainvoke(
        "Return JSON matching {symbol: string, should_read: boolean} for this request: "
        "read the market snapshot for AAPL."
    )
    structured = QuoteIntent.model_validate(structured_response)

    agent = components.agent_factory.build(profile, model=model)
    context = ExecutionContext(
        tenant_id="stage1-provider-probe",
        subject_id="stage1-provider-probe",
        scopes={"market:read", "tools:read"},
        turn_id="turn-stage1-provider-probe",
        run_id="run-stage1-provider-probe",
        data_classification="internal",
    )
    await agent.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "You must call market_snapshot exactly once for AAPL, then briefly report "
                        "its returned provider and as-of timestamp."
                    ),
                }
            ]
        },
        context=context,
        config={"configurable": {"thread_id": "stage1-provider-probe"}},
    )
    governed_tool_executed = any(
        record.event_type is AuditEventType.FINANCIAL_TOOL_EXECUTED
        and record.resource_id == "market_snapshot"
        for record in audit.records()
    )
    run_tree = get_current_run_tree()
    trace_url = run_tree.get_url() if run_tree is not None else None
    return Stage1ProviderProbeResult(
        model_type=model._llm_type,
        tool_calling=bool(tool_response.tool_calls),
        structured_output=structured.model_dump(mode="json"),
        governed_tool_executed=governed_tool_executed,
        trace_url=trace_url,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    result = asyncio.run(probe_provider(FinanceClawSettings()))
    if not result.tool_calling or not result.governed_tool_executed:
        raise SystemExit(f"Stage-1 Provider probe failed: {result!r}")
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
