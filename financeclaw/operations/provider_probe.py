"""提供 provider probe 运维命令的可调用入口。"""

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass

from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from pydantic import BaseModel, Field

from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.kernel import ExecutionContext
from financeclaw.modules.audit import AuditEventType, InMemoryAuditRepository


class QuoteIntent(BaseModel):
    """定义QuoteIntent。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        symbol: 标准化金融标的代码。
        should_read: 结构化探测输出中，模型是否判断当前请求需要读取行情。
    """

    symbol: str = Field(min_length=1, max_length=16)
    should_read: bool


@dataclass(frozen=True, slots=True)
class Stage1ProviderProbeResult:
    """定义Stage1ProviderProbe的执行结果。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        model_type: 实际创建的 LangChain 聊天模型实现类型。
        tool_calling: 探测结果是否证明模型能够产生结构化工具调用。
        structured_output: 模型结构化输出能力的探测结果。
        governed_tool_executed: 探测中的工具是否确实经过治理链并成功执行。
        trace_url: 本次探测对应的可观测性追踪链接；不可用时为空。
    """

    model_type: str
    tool_calling: bool
    structured_output: dict[str, object]
    governed_tool_executed: bool
    trace_url: str | None


@traceable(name="stage1.provider.probe", run_type="chain", tags=["stage:1"])
async def probe_provider(settings: FinanceClawSettings) -> Stage1ProviderProbeResult:
    """验证模型工具调用、结构化输出、治理执行和追踪链路。"""
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
    """解析命令行参数，执行 provider probe 操作并输出结果。"""
    parser = argparse.ArgumentParser()
    parser.parse_args()
    result = asyncio.run(probe_provider(FinanceClawSettings()))
    if not result.tool_calling or not result.governed_tool_executed:
        raise SystemExit(f"Stage-1 Provider probe failed: {result!r}")
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
