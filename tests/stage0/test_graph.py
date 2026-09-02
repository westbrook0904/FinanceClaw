from typing import Any, ClassVar

import pytest
from langgraph.types import Command

from financeclaw_spike.context import SpikeContext
from financeclaw_spike.graph import create_demo_agent
from financeclaw_spike.offline_model import OfflineSpikeModel
from financeclaw_spike.settings import SpikeSettings
from financeclaw_spike.tools import DemoReadTool, DemoWriteTool


def _settings() -> SpikeSettings:
    return SpikeSettings(
        environment="test",
        offline_model=True,
        debug_full_io=False,
        read_retry_initial_delay=0,
    )


@pytest.mark.asyncio
async def test_read_succeeds_after_configured_tool_retry_and_streams() -> None:
    read_tool = DemoReadTool(fail_first=1)
    agent = create_demo_agent(settings=_settings(), read_tool=read_tool)
    config = {"configurable": {"thread_id": "stage0-read"}}
    context = SpikeContext(request_id="read", environment="test")

    parts = [
        part
        async for part in agent.astream(
            {"messages": [{"role": "user", "content": "read AAPL"}]},
            config=config,
            context=context,
            stream_mode="updates",
            version="v2",
        )
    ]
    state = await agent.aget_state(config)

    assert parts
    assert read_tool.call_count == 2
    assert len(state.values["messages"]) == 4
    assert state.next == ()


def test_write_interrupts_then_approve_executes_once() -> None:
    write_tool = DemoWriteTool()
    agent = create_demo_agent(settings=_settings(), write_tool=write_tool)
    config = {"configurable": {"thread_id": "stage0-approve"}}
    context = SpikeContext(request_id="approve", environment="test")

    interrupted = agent.invoke(
        {"messages": [{"role": "user", "content": "write watchlist"}]},
        config=config,
        context=context,
        version="v2",
    )

    assert interrupted.interrupts
    assert write_tool.writes == ()
    assert agent.get_state(config).next

    resumed = agent.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config=config,
        context=context,
        version="v2",
    )

    assert resumed.interrupts == ()
    assert write_tool.writes == ({"symbol": "AAPL", "note": "stage0"},)
    assert agent.get_state(config).next == ()


def test_write_reject_does_not_execute() -> None:
    write_tool = DemoWriteTool()
    agent = create_demo_agent(settings=_settings(), write_tool=write_tool)
    config = {"configurable": {"thread_id": "stage0-reject"}}
    context = SpikeContext(request_id="reject", environment="test")

    agent.invoke(
        {"messages": [{"role": "user", "content": "write watchlist"}]},
        config=config,
        context=context,
        version="v2",
    )
    resumed = agent.invoke(
        Command(resume={"decisions": [{"type": "reject", "message": "not approved"}]}),
        config=config,
        context=context,
        version="v2",
    )

    assert resumed.interrupts == ()
    assert write_tool.writes == ()


def test_dynamic_tool_filter_hides_write_tool() -> None:
    write_tool = DemoWriteTool()
    agent = create_demo_agent(
        settings=_settings(),
        model=OfflineSpikeModel(),
        write_tool=write_tool,
    )
    config = {"configurable": {"thread_id": "stage0-filter"}}
    context = SpikeContext(request_id="filter", allow_write=False, environment="test")

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "write watchlist"}]},
        config=config,
        context=context,
        version="v2",
    )

    assert result.interrupts == ()
    assert write_tool.writes == ()
    assert "not authorized" in result.value["messages"][-1].content


def test_model_retry_recovers_transient_provider_failure() -> None:
    class FlakyModel(OfflineSpikeModel):
        call_count: ClassVar[int] = 0

        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            type(self).call_count += 1
            if type(self).call_count <= 2:
                raise ConnectionError("transient provider failure")
            return super()._generate(*args, **kwargs)

    agent = create_demo_agent(settings=_settings(), model=FlakyModel())
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "read AAPL"}]},
        config={"configurable": {"thread_id": "stage0-model-retry"}},
        version="v2",
    )

    assert result.interrupts == ()
    assert FlakyModel.call_count == 4


def test_model_fallback_recovers_primary_failure() -> None:
    class FailingModel(OfflineSpikeModel):
        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("primary provider unavailable")

    agent = create_demo_agent(
        settings=_settings(),
        model=FailingModel(),
        fallback_model=OfflineSpikeModel(),
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "read AAPL"}]},
        config={"configurable": {"thread_id": "stage0-model-fallback"}},
        version="v2",
    )

    assert result.interrupts == ()
    assert "stage0-demo" in result.value["messages"][-1].content
