"""`test_governance` 模块提供`stage1`相关能力。"""

from types import MappingProxyType

import pytest
from pydantic import ValidationError

from financeclaw.kernel import DataClassification, ExecutionContext
from financeclaw.orchestration.tools import (
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
    """处理 `当前操作`，并返回边界约定的结果。"""
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
    """处理 `当前操作`，并返回边界约定的结果。"""
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
    """验证函数名所描述的业务场景符合预期。"""
    with pytest.raises(ValidationError, match="must always require approval"):
        governance(side_effect="write", retry_profile="none")
    with pytest.raises(ValidationError, match="cannot use automatic retry"):
        governance(side_effect="write", approval="always")


def test_catalog_is_immutable_versioned_and_rejects_conflicts() -> None:
    """验证函数名所描述的业务场景符合预期。"""
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
    """验证函数名所描述的业务场景符合预期。"""
    # 准备 policy，供后续步骤使用。
    policy = ToolPolicy()
    # 准备 allowed，供后续步骤使用。
    allowed = governance(tenant_allowlist={"tenant-a"})

    # 继续执行前验证内部不变量。
    assert policy.evaluate(context(), allowed, {}).effect is ToolDecisionType.ALLOW
    # 继续执行前验证内部不变量。
    assert policy.evaluate(context(scopes=set()), allowed, {}).effect is ToolDecisionType.DENY
    # 继续执行前验证内部不变量。
    assert (
        policy.evaluate(context(tenant_id="tenant-b"), allowed, {}).effect is ToolDecisionType.DENY
    )
    # 继续执行前验证内部不变量。
    assert (
        policy.evaluate(context(data_classification="restricted"), allowed, {}).effect
        is ToolDecisionType.DENY
    )
    # 准备 approval，供后续步骤使用。
    approval = governance(
        tool_id="write",
        side_effect="write",
        idempotency="key_required",
        approval="always",
        retry_profile="none",
    )
    # 继续执行前验证内部不变量。
    assert policy.evaluate(context(), approval, {}).effect is ToolDecisionType.REQUIRE_APPROVAL


def test_retry_predicate_is_read_only_and_exception_specific() -> None:
    """验证函数名所描述的业务场景符合预期。"""
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
