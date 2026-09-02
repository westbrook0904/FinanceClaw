"""LangChain agent compiled as the isolated Stage-0 LangGraph graph."""

from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelFallbackMiddleware,
    ModelRetryMiddleware,
    ToolRetryMiddleware,
)
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from financeclaw_spike.context import SpikeContext
from financeclaw_spike.observability import (
    ContextTraceMiddleware,
    DynamicToolFilterMiddleware,
    FullPromptDebugMiddleware,
)
from financeclaw_spike.offline_model import OfflineSpikeModel
from financeclaw_spike.settings import SpikeSettings
from financeclaw_spike.tools import DemoReadTool, DemoWriteTool, TransientReadError

SYSTEM_PROMPT = """You are the FinanceClaw Stage-0 compatibility agent.
Use read_market_snapshot for read-only demo facts. Use write_watchlist only when asked to
change the demo watchlist. Never invent tool results and never include credentials in output.
"""

_DEFAULT_CHECKPOINTER = object()


def _provider_model(settings: SpikeSettings) -> BaseChatModel:
    model = init_chat_model(settings.model, temperature=0)
    if not isinstance(model, BaseChatModel):
        raise TypeError("configured provider did not create a BaseChatModel")
    return model


def create_demo_agent(
    *,
    settings: SpikeSettings | None = None,
    model: BaseChatModel | None = None,
    fallback_model: BaseChatModel | None = None,
    checkpointer: Any = _DEFAULT_CHECKPOINTER,
    read_tool: DemoReadTool | None = None,
    write_tool: DemoWriteTool | None = None,
) -> Any:
    """Build the spike without importing any legacy Harness Runtime packages."""

    settings = settings or SpikeSettings()
    model = model or (OfflineSpikeModel() if settings.offline_model else _provider_model(settings))
    if fallback_model is None and settings.fallback_model:
        fallback_model = init_chat_model(settings.fallback_model, temperature=0)
    read_tool = read_tool or DemoReadTool()
    write_tool = write_tool or DemoWriteTool()

    middleware: list[Any] = [
        DynamicToolFilterMiddleware(),
        ContextTraceMiddleware(),
        FullPromptDebugMiddleware(enabled=settings.debug_full_io),
        ModelRetryMiddleware(
            max_retries=2,
            initial_delay=0.1,
            jitter=False,
            on_failure="error",
        ),
        ToolRetryMiddleware(
            max_retries=settings.read_max_retries,
            tools=[read_tool.name],
            retry_on=(TransientReadError,),
            initial_delay=settings.read_retry_initial_delay,
            jitter=False,
            on_failure="error",
        ),
        HumanInTheLoopMiddleware(
            interrupt_on={
                read_tool.name: False,
                write_tool.name: {"allowed_decisions": ["approve", "reject"]},
            },
            description_prefix="FinanceClaw Stage-0 WRITE approval required",
        ),
    ]
    if fallback_model is not None:
        middleware.insert(3, ModelFallbackMiddleware(fallback_model))

    resolved_checkpointer = (
        InMemorySaver() if checkpointer is _DEFAULT_CHECKPOINTER else checkpointer
    )
    return create_agent(
        model=model,
        tools=[read_tool, write_tool],
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
        context_schema=SpikeContext,
        checkpointer=resolved_checkpointer,
        name="demo_agent",
    )


def make_graph(config: RunnableConfig) -> Any:
    """Agent Server graph factory; the server supplies durable checkpointing."""

    del config
    return create_demo_agent(settings=SpikeSettings(), checkpointer=None)
