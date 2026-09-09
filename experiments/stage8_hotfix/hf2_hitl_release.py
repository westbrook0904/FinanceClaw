"""Synthetic HITL publication for HF-2 probes; never registered by product catalogs."""

import json
from dataclasses import replace

from financeclaw.kernel.agents import AgentProfileCatalog, ToolRef
from financeclaw.shared.releases.fingerprint import configuration_fingerprint
from financeclaw.shared.releases.subgraphs import worker_declaration


def with_test_hitl(catalogs):
    """Pin a synthetic write leaf inside the research Worker for native approval coverage."""
    worker = catalogs.agent_profiles.resolve("market_research_agent", "1.3.0").model_copy(
        update={
            "allowed_tools": (ToolRef(tool_id="watchlist_add", version="1.0.0"),),
            "deployment_revision": "hf2-synthetic-hitl/1",
        }
    )
    declaration = worker_declaration(worker, catalogs.tool_catalog, catalogs.model_profiles)
    root = catalogs.agent_profiles.resolve("finance_agent", "1.5.0")
    manifest = tuple(
        declaration if json.loads(item)["target_id"] == "market_research_agent" else item
        for item in root.worker_manifest
    )
    root = root.model_copy(
        update={
            "worker_manifest": manifest,
            "deployment_revision": "hf2-synthetic-hitl/1",
            "configuration_fingerprint": configuration_fingerprint(
                root, manifest, "hf2-synthetic-hitl/1"
            ),
        }
    )
    profiles = AgentProfileCatalog(
        [p for p in catalogs.agent_profiles.values() if p.key not in {root.key, worker.key}]
        + [root, worker]
    )
    return replace(catalogs, agent_profiles=profiles)
