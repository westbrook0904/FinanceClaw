"""配置真实 Provider 与 LangSmith 后执行的在线门禁探针命令（Stage-1）。

在真实模型 Provider 上验证三项能力：工具调用（tool calling）、结构化输出
（json_mode）以及经治理的金融工具执行（以 Audit 记录佐证），探针过程作为
一条 LangSmith chain 追踪上报。运行方式：
``python -m financeclaw.operations.provider_probe``（需真实 Provider 配置）。
"""

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
    """Provider 结构化输出探针的目标契约：行情读取意图。

    使用场景：``probe_provider`` 以 ``json_mode`` 让真实模型把自然语言请求
    解析为本模型，用于验证 Provider 的结构化输出能力。

    Attributes:
        symbol: 标的代码，长度 1 至 16。
        should_read: 模型判断是否应当读取该标的的行情。

    """

    symbol: str = Field(min_length=1, max_length=16)
    should_read: bool


@dataclass(frozen=True, slots=True)
class Stage1ProviderProbeResult:
    """Stage-1 Provider 在线探针的结果快照。

    使用场景：``probe_provider`` 返回后由 ``main`` 汇总打印，并据此判定
    在线门禁是否通过（必须具备工具调用且受治理工具已执行）。

    Attributes:
        model_type: 模型适配器的类型标识（LangChain ``_llm_type``）。
        tool_calling: 模型是否成功发起工具调用。
        structured_output: 结构化输出解析后的 QuoteIntent JSON。
        governed_tool_executed: 受治理的 ``market_snapshot`` 是否留有 Audit 记录。
        trace_url: 本次探针的 LangSmith 追踪链接；不在追踪上下文时为 None。

    """

    model_type: str
    tool_calling: bool
    structured_output: dict[str, object]
    governed_tool_executed: bool
    trace_url: str | None


@traceable(name="stage1.provider.probe", run_type="chain", tags=["stage:1"])
async def probe_provider(settings: FinanceClawSettings) -> Stage1ProviderProbeResult:
    """在真实 Provider 上执行工具调用、结构化输出与治理工具三项探针。

    Args:
        settings: FinanceClaw 运行设置（含真实 Provider 与 LangSmith 配置）。

    Returns:
        汇总三项探针结论与 LangSmith 追踪链接的结果对象。

    Raises:
        pydantic.ValidationError: 结构化输出不符合 ``QuoteIntent`` 契约。

    """
    # 1. 以内存审计仓储构建组件，解析默认 Agent Profile、模型与行情工具。
    audit = InMemoryAuditRepository()
    components = build_components(settings, audit=audit)
    profile = components.default_agent_profile
    model = components.model_factory.create(profile.model_profile)
    market_tool = components.tool_catalog.resolve("market_snapshot").tool
    # 2. 探针工具调用：要求模型必须调用 market_snapshot。
    tool_response = await model.bind_tools([market_tool]).ainvoke(
        "Call market_snapshot for AAPL. Do not answer without making that tool call."
    )
    # 3. 探针结构化输出：以 json_mode 把请求解析为 QuoteIntent。
    structured_response = await model.with_structured_output(
        QuoteIntent, method="json_mode"
    ).ainvoke(
        "Return JSON matching {symbol: string, should_read: boolean} for this request: "
        "read the market snapshot for AAPL."
    )
    structured = QuoteIntent.model_validate(structured_response)
    # 4. 探针受治理执行：Agent 真实调用 market_snapshot 并落 Audit 记录。
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
    # 5. 汇总结果并取当前 LangSmith 追踪链接。
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
    """执行在线 Provider 探针，在线门禁不通过时以非零码退出。"""
    # 1. 解析命令行参数（当前无必填项，保留扩展位）。
    parser = argparse.ArgumentParser()
    parser.parse_args()
    # 2. 执行探针。
    result = asyncio.run(probe_provider(FinanceClawSettings()))
    # 3. 校验在线门禁：必须具备工具调用且受治理工具已执行。
    if not result.tool_calling or not result.governed_tool_executed:
        raise SystemExit(f"Stage-1 Provider probe failed: {result!r}")
    # 4. 打印 JSON 结果。
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
