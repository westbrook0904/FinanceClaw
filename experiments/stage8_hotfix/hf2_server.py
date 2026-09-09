"""Isolated native HF-2 server: production subgraphs and credential-free scripted models."""

import os
from pathlib import Path

from financeclaw.agent_server.bootstrap import build_components
from financeclaw.agent_server.memory.service import LongTermMemoryService
from financeclaw.agent_server.tools.catalog import ToolCatalog
from financeclaw.agent_server.tools.local import default_local_tools
from financeclaw.agent_server.tools.mcp import managed_mcp_quote_tool
from financeclaw.agent_server.tools.memory import default_memory_tools
from financeclaw.shared.infrastructure.resources import build_resources
from tests.stage6fix.test_batch_tools import call
from tests.stage7.support import request
from tests.stage8_hotfix.test_bff_runs import config
from tests.stage8_hotfix.test_production_subgraphs import (
    FreshMarket,
    ResearchModel,
    SerialModel,
    portfolio,
)

settings = config(Path.cwd())
resources = build_resources(settings, enable_persistence=True)
memory = LongTermMemoryService(
    conversation_repository=resources.conversation_repository, audit=resources.audit
)
components = build_components(
    settings,
    resources=resources,
    enable_persistence=True,
    resource_concurrency=1,
    tool_catalog=ToolCatalog(
        (
            *default_local_tools(market_tool=FreshMarket()),
            managed_mcp_quote_tool(timeout_seconds=settings.mcp_timeout_seconds),
            *default_memory_tools(memory),
        )
    ),
)
components.agent_factory.memory_service = None
if os.environ.get("HF2_SCENARIO") == "hitl":
    import json

    from experiments.stage8_hotfix.hf2_hitl_release import with_test_hitl
    from financeclaw.shared.releases.catalog import ReleaseCatalogs
    from tests.stage8_hotfix.test_production_subgraphs import HITLModel

    releases = with_test_hitl(
        ReleaseCatalogs(
            components.model_profiles,
            components.agent_profiles,
            components.tool_catalog,
            components.workflow_catalog,
        )
    )
    research = releases.agent_profiles.resolve("market_research_agent", "1.3.0")
    profile = releases.agent_profiles.resolve("finance_agent", "1.5.0")
    worker = components.tool_catalog.resolve("call_agent__market_research_agent", "1.3.0").tool
    worker.release = research
    worker.declaration = next(
        item
        for item in profile.worker_manifest
        if json.loads(item)["target_id"] == "market_research_agent"
    )
    worker.graph = components.agent_factory.build(
        research, model=HITLModel(), checkpointer=None, fallback_models=()
    )
    root = components.agent_factory.build(
        profile,
        model=SerialModel(
            calls=[
                call("call_agent__market_research_agent", 1, task="synthetic approve"),
                call("call_agent__market_research_agent", 2, task="synthetic reject"),
            ]
        ),
        checkpointer=None,
        fallback_models=(),
    )
else:
    research = components.agent_profiles.resolve("market_research_agent", "1.3.0")
    worker = components.tool_catalog.resolve("call_agent__market_research_agent", "1.3.0").tool
    worker.graph = components.agent_factory.build(
        research, model=ResearchModel(questions=2), checkpointer=None, fallback_models=()
    )
    root = components.agent_factory.build(
        components.agent_profiles.resolve("finance_agent", "1.5.0"),
        model=SerialModel(
            calls=[
                call("call_agent__market_research_agent", 1, task="bounded synthetic research"),
                portfolio(2),
                call(
                    "call_agent__ziwei_doushu_agent",
                    3,
                    task="合成盘面",
                    arguments=request(mode="interpretation").model_dump(mode="json"),
                ),
                portfolio(4),
            ]
        ),
        checkpointer=None,
        fallback_models=(),
    )
