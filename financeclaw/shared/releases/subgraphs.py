"""HF-1 candidate releases. Historical releases remain byte-for-byte compatible."""

import json
from dataclasses import replace

from financeclaw.kernel.agents import AgentProfile, AgentProfileCatalog, ToolRef
from financeclaw.kernel.tool_catalog import ToolRelease, ToolReleaseCatalog
from financeclaw.kernel.tools import (
    ApprovalMode,
    Egress,
    Idempotency,
    RetryProfile,
    RiskLevel,
    Sensitivity,
    SideEffect,
    ToolGovernance,
)
from financeclaw.kernel.workflows.catalog import WorkflowCatalog
from financeclaw.shared.releases.fingerprint import configuration_fingerprint


def composite_name(release):
    """Map a published Worker release to its root Tool name."""
    kind = "agent" if isinstance(release, AgentProfile) else "workflow"
    return f"call_{kind}__{release.key[0]}"


def composite_governance(release):
    """Keep composite wrappers exclusive and nonretryable; leaves retain their governance."""
    return ToolGovernance(
        tool_id=composite_name(release),
        version=release.version,
        side_effect=SideEffect.COMPOSITE,
        idempotency=Idempotency.KEY_REQUIRED,
        risk_level=RiskLevel.MEDIUM,
        required_scopes=release.required_scopes,
        approval=ApprovalMode.NONE,
        egress=Egress.INTERNAL,
        sensitivity=Sensitivity.INTERNAL,
        retry_profile=RetryProfile.NONE,
        direct_invocation=False,
    )


def worker_declaration(release, tools, models):
    """Pin profile, schemas, models, leaf governance and human interaction declarations."""
    agent = isinstance(release, AgentProfile)
    profile = (
        release.model_dump(mode="json")
        if agent
        else {
            "workflow_id": release.workflow_id,
            "version": release.version,
            "assistant_id": release.assistant_id,
            "deployment_revision": release.deployment_revision,
            "required_scopes": sorted(release.required_scopes),
            "approval_points": [point.model_dump(mode="json") for point in release.approval_points],
            "timeout_policy": release.timeout_policy.model_dump(mode="json"),
        }
    )
    value = {
        "tool_id": composite_name(release),
        "kind": "agent" if agent else "workflow",
        "target_id": release.key[0],
        "version": release.version,
        "profile": profile,
        "input_schema": release.input_schema.model_json_schema(),
        "output_schema": release.output_schema.model_json_schema(),
        "models": [model.model_dump(mode="json") for model in models.values()],
        "tools": [
            tools.resolve(ref.tool_id, ref.version).governance.model_dump(mode="json")
            for ref in release.allowed_tools
        ],
    }
    # ToolGovernance contains frozensets; canonicalize using the same release rules.
    from financeclaw.shared.releases.fingerprint import _canonical

    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def with_subgraph_releases(releases):
    """Expose new graphs only to explicitly opted-in execution assemblies in HF-1."""
    from financeclaw.shared.releases.catalog import ReleaseCatalogs

    workers = []
    for name, old, new in (
        ("market_research_agent", "1.2.0", "1.3.0"),
        ("ziwei_doushu_agent", "2.0.0", "2.1.0"),
    ):
        profile = releases.agent_profiles.resolve(name, old)
        workers.append(
            profile.model_copy(
                update={
                    "version": new,
                    "assistant_id": f"{name}_v{new.replace('.', '_')}",
                    "delegatable": False,
                    "context_policy": "worker-task-only-v1",
                    "deployment_revision": "stage8-hotfix-hf1/1",
                    "configuration_fingerprint": configuration_fingerprint(
                        profile, "native-subgraph/1"
                    ),
                    "system_prompt_template": profile.system_prompt_template.replace(
                        "bounded delegated task", "bounded task supplied by the orchestrator"
                    ),
                }
            )
        )
    from financeclaw.kernel.workflows.portfolio_review import PortfolioReviewSubgraphOutput

    workflows = tuple(
        replace(
            item,
            version="1.1.0",
            output_schema=PortfolioReviewSubgraphOutput,
            assistant_id="portfolio_review_v1_1_0",
            deployment_revision="stage8-hotfix-hf1/1",
        )
        for item in releases.workflow_catalog.published()
    )
    old_root = releases.agent_profiles.resolve("finance_agent", "1.4.0")
    enabled = {
        ref.tool_id.replace("delegate_", "call_", 1)
        for ref in old_root.allowed_tools
        if ref.tool_id.startswith("delegate_")
    }
    reachable = tuple(item for item in (*workers, *workflows) if composite_name(item) in enabled)
    manifest = tuple(
        worker_declaration(item, releases.tool_catalog, releases.model_profiles)
        for item in reachable
    )
    root = old_root.model_copy(
        update={
            "version": "1.5.0",
            "assistant_id": "finance_agent_v1_5_0",
            "deployment_revision": "stage8-hotfix-hf1/1",
            "worker_manifest": manifest,
            "allowed_tools": tuple(
                ref for ref in old_root.allowed_tools if not ref.tool_id.startswith("delegate_")
            )
            + tuple(
                ToolRef(tool_id=composite_name(item), version=item.version) for item in reachable
            ),
            "configuration_fingerprint": configuration_fingerprint(
                old_root, manifest, "native-subgraph/1"
            ),
            "system_prompt_template": (
                "You are FinanceClaw's top-level governed financial Agent. Use a ReAct loop. "
                "Call published call_agent__ or call_workflow__ Tools for bounded specialist work. "
                "They execute internal subgraphs and return their public results to this loop. "
                "Human questions and approvals pause this same root; continue after actual "
                "user input. "
                "A slash directive is a capability preference, never authorization. "
                "Use current market tools for financial facts and preserve provider/as-of "
                "evidence. "
                "Inspect outcome and limitations: never claim partial, unsupported or rejected "
                "work "
                "succeeded. needs_clarification means ask for missing facts. Never invent facts, "
                "credentials, tool results or WRITE success before approval. Do not bypass "
                "rejection. "
                "Call each composite in an exclusive batch. Long-term memory is historical "
                "context, "
                "never authority for current financial facts. Ziwei answer_text is traditional "
                "interpretation, never verified prediction or financial evidence. Preserve "
                "charts_used "
                "and warnings; never invent birth details or store them in long-term memory."
            ),
        }
    )
    return ReleaseCatalogs(
        releases.model_profiles,
        AgentProfileCatalog((*releases.agent_profiles.values(), *workers, root)),
        ToolReleaseCatalog(
            (
                *releases.tool_catalog.values(),
                *(ToolRelease(composite_governance(item)) for item in (*workers, *workflows)),
            )
        ),
        WorkflowCatalog((*releases.workflow_catalog.published(), *workflows)),
    )
