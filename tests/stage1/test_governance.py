from types import MappingProxyType

import pytest
from pydantic import ValidationError

from financeclaw.contracts import DataClassification, ExecutionContext
from financeclaw.tools import (
    ApprovalMode,
    AuditLevel,
    Egress,
    Idempotency,
    ManagedTool,
    MarketSnapshotTool,
    RetryProfile,
    RiskLevel,
    Sensitivity,
    SideEffect,
    ToolCatalog,
    ToolDecisionType,
    ToolGovernance,
    ToolPolicy,
    TransientToolError,
)


def governance(**overrides: object) -> ToolGovernance:
    values = {
        "tool_id": "market_snapshot",
        "version": "1.0.0",
        "side_effect": SideEffect.READ,
        "idempotency": Idempotency.IDEMPOTENT,
        "risk_level": RiskLevel.LOW,
        "required_scopes": frozenset({"market:read"}),
        "approval": ApprovalMode.NONE,
        "egress": Egress.INTERNAL,
        "sensitivity": Sensitivity.CONFIDENTIAL,
        "retry_profile": RetryProfile.TRANSIENT_READ,
        "audit_level": AuditLevel.FULL,
        "allowed_data_classes": frozenset({DataClassification.INTERNAL}),
    }
    values.update(overrides)
    return ToolGovernance.model_validate(values)


def context(**overrides: object) -> ExecutionContext:
    values = {
        "tenant_id": "tenant-a",
        "subject_id": "subject-a",
        "scopes": {"market:read"},
        "turn_id": "turn-a",
        "run_id": "run-a",
        "data_classification": "internal",
    }
    values.update(overrides)
    return ExecutionContext.model_validate(values)


def test_governance_rejects_unapproved_or_retryable_write() -> None:
    with pytest.raises(ValidationError, match="must always require approval"):
        governance(side_effect="write", retry_profile="none")
    with pytest.raises(ValidationError, match="cannot use automatic retry"):
        governance(side_effect="write", approval="always")


def test_catalog_is_immutable_versioned_and_rejects_conflicts() -> None:
    v1 = ManagedTool(MarketSnapshotTool(), governance())
    v2 = ManagedTool(MarketSnapshotTool(), governance(version="1.1.0"))
    catalog = ToolCatalog((v1, v2))

    assert catalog.resolve("market_snapshot").governance.version == "1.1.0"
    assert isinstance(catalog._entries, MappingProxyType)
    with pytest.raises(TypeError):
        catalog._entries[("market_snapshot", "2.0.0")] = v2  # type: ignore[index]
    with pytest.raises(ValueError, match="duplicate tool version"):
        ToolCatalog((v1, v1))


def test_policy_covers_scope_tenant_data_and_approval() -> None:
    policy = ToolPolicy()
    allowed = governance(tenant_allowlist={"tenant-a"})

    assert policy.evaluate(context(), allowed, {}).effect is ToolDecisionType.ALLOW
    assert policy.evaluate(context(scopes=set()), allowed, {}).effect is ToolDecisionType.DENY
    assert (
        policy.evaluate(context(tenant_id="tenant-b"), allowed, {}).effect is ToolDecisionType.DENY
    )
    assert (
        policy.evaluate(context(data_classification="restricted"), allowed, {}).effect
        is ToolDecisionType.DENY
    )
    approval = governance(
        tool_id="write",
        side_effect="write",
        idempotency="key_required",
        approval="always",
        retry_profile="none",
    )
    assert policy.evaluate(context(), approval, {}).effect is ToolDecisionType.REQUIRE_APPROVAL


def test_retry_predicate_is_read_only_and_exception_specific() -> None:
    policy = ToolPolicy()
    read = governance()
    write = governance(
        tool_id="write",
        side_effect="write",
        idempotency="key_required",
        approval="always",
        retry_profile="none",
    )

    assert policy.retryable(read, TransientToolError("temporary"), {})
    assert not policy.retryable(read, ValueError("bad input"), {})
    assert not policy.retryable(write, TransientToolError("temporary"), {})
