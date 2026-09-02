"""Credential-gated real Provider and structured-output probe."""

from dataclasses import dataclass

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langsmith import traceable
from pydantic import BaseModel, Field

from financeclaw_spike.settings import SpikeSettings
from financeclaw_spike.tools import DemoReadTool


class StructuredQuoteIntent(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    should_read: bool


@dataclass(frozen=True, slots=True)
class ProviderProbeResult:
    model_type: str
    tool_calling: bool
    structured_output: StructuredQuoteIntent


@traceable(name="stage0.provider.probe", run_type="chain", tags=["stage:0", "provider:openai"])
async def probe_provider(settings: SpikeSettings) -> ProviderProbeResult:
    model = init_chat_model(settings.model, temperature=0)
    if not isinstance(model, BaseChatModel):
        raise TypeError("configured provider did not create a BaseChatModel")

    tool_response = await model.bind_tools([DemoReadTool()]).ainvoke(
        "Call read_market_snapshot for AAPL. Do not answer without the tool."
    )
    structured_model = model.with_structured_output(StructuredQuoteIntent)
    structured = await structured_model.ainvoke(
        "Extract this request: read the market snapshot for AAPL."
    )
    return ProviderProbeResult(
        model_type=model._llm_type,
        tool_calling=bool(tool_response.tool_calls),
        structured_output=structured,
    )
