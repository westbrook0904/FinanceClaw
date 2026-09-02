import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from financeclaw_spike.context import SpikeContext
from financeclaw_spike.graph import _provider_model, create_demo_agent
from financeclaw_spike.infrastructure import probe_services
from financeclaw_spike.offline_model import OfflineSpikeModel
from financeclaw_spike.provider import probe_provider
from financeclaw_spike.settings import SpikeSettings
from financeclaw_spike.tools import DemoWriteTool


def test_python_and_locked_framework_imports() -> None:
    assert sys.version_info[:2] == (3, 13)
    assert version("langchain") == "1.3.18"
    assert version("langgraph") == "1.2.11"
    assert version("langsmith") == "0.12.1"
    assert version("langchain-mcp-adapters") == "0.3.2"
    assert version("langgraph-checkpoint-postgres") == "3.1.2"
    assert version("langgraph-checkpoint-redis") == "0.5.2"


def test_langgraph_config_exports_graph_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCECLAW_SPIKE_OFFLINE_MODEL", "true")
    config = json.loads(Path("langgraph.json").read_text())

    assert config["python_version"] == "3.13"
    assert config["graphs"] == {"demo_agent": "./financeclaw_spike/graph.py:make_graph"}


def test_real_provider_integration_constructs_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "stage0-placeholder-not-a-real-key")

    model = _provider_model(SpikeSettings(environment="test", debug_full_io=False))

    assert isinstance(model, BaseChatModel)
    assert model._llm_type == "openai-chat"


@pytest.mark.external
@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not configured")
async def test_live_provider_tool_calling_and_structured_output() -> None:
    result = await probe_provider(SpikeSettings(environment="test", debug_full_io=False))

    assert result.tool_calling
    assert result.structured_output.symbol == "AAPL"
    assert result.structured_output.should_read


@pytest.mark.external
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("FINANCECLAW_SPIKE_POSTGRES_DSN") or not os.getenv("FINANCECLAW_SPIKE_REDIS_URL"),
    reason="PostgreSQL/Redis probe settings are not configured",
)
async def test_postgres_redis_checkpoint_and_store() -> None:
    result = await probe_services(SpikeSettings(environment="test", debug_full_io=False))

    assert result.postgres
    assert result.redis
    assert result.checkpoint
    assert result.store


@pytest.mark.external
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("FINANCECLAW_SPIKE_POSTGRES_DSN"),
    reason="PostgreSQL probe setting is not configured",
)
async def test_postgres_checkpoint_resumes_after_graph_rebuild() -> None:
    dsn = os.environ["FINANCECLAW_SPIKE_POSTGRES_DSN"]
    settings = SpikeSettings(environment="test", offline_model=True, debug_full_io=False)
    config = {"configurable": {"thread_id": "stage0-postgres-resume"}}
    context = SpikeContext(request_id="postgres-resume", environment="test")

    async with AsyncPostgresSaver.from_conn_string(dsn) as first_checkpointer:
        await first_checkpointer.setup()
        first_write_tool = DemoWriteTool()
        first_agent = create_demo_agent(
            settings=settings,
            model=OfflineSpikeModel(),
            write_tool=first_write_tool,
            checkpointer=first_checkpointer,
        )
        interrupted = await first_agent.ainvoke(
            {"messages": [{"role": "user", "content": "write watchlist"}]},
            config=config,
            context=context,
            version="v2",
        )
        assert interrupted.interrupts
        assert first_write_tool.writes == ()

    async with AsyncPostgresSaver.from_conn_string(dsn) as restarted_checkpointer:
        restarted_write_tool = DemoWriteTool()
        restarted_agent = create_demo_agent(
            settings=settings,
            model=OfflineSpikeModel(),
            write_tool=restarted_write_tool,
            checkpointer=restarted_checkpointer,
        )
        resumed = await restarted_agent.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
            context=context,
            version="v2",
        )

    assert resumed.interrupts == ()
    assert restarted_write_tool.writes == ({"symbol": "AAPL", "note": "stage0"},)
