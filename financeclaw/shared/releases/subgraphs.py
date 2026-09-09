"""Canonical declarations for the current internal Worker releases."""

import json

from financeclaw.kernel.agents import AgentProfile
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
        "models": [model.model_dump(mode="python") for model in models.values()],
        "tools": [
            tools.resolve(ref.tool_id, ref.version).governance.model_dump(mode="python")
            for ref in release.allowed_tools
        ],
    }
    # ToolGovernance contains frozensets; canonicalize using the same release rules.
    from financeclaw.shared.releases.fingerprint import _canonical

    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
