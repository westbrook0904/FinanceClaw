"""工具治理的唯一发布声明；BFF 无须加载工具实现。"""

from financeclaw.kernel.context import DataClassification
from financeclaw.kernel.tools import (
    ApprovalMode,
    AuditLevel,
    Egress,
    Idempotency,
    RetryProfile,
    RiskLevel,
    Sensitivity,
    SideEffect,
    ToolGovernance,
)
from financeclaw.kernel.ziwei_tools import ZIWEI_TOOL_INPUTS


def local_tool_governance() -> tuple[ToolGovernance, ...]:
    """返回 local 工具的治理声明，不创建执行实例。"""
    common_data_classes = frozenset(
        {DataClassification.PUBLIC, DataClassification.INTERNAL, DataClassification.CONFIDENTIAL}
    )
    return (
        ToolGovernance(
            tool_id="market_snapshot",
            version="1.0.0",
            side_effect=SideEffect.READ,
            idempotency=Idempotency.IDEMPOTENT,
            risk_level=RiskLevel.LOW,
            required_scopes=frozenset({"market:read"}),
            approval=ApprovalMode.NONE,
            egress=Egress.INTERNAL,
            sensitivity=Sensitivity.CONFIDENTIAL,
            retry_profile=RetryProfile.TRANSIENT_READ,
            audit_level=AuditLevel.FULL,
            allowed_data_classes=common_data_classes,
        ),
        ToolGovernance(
            tool_id="watchlist_add",
            version="1.0.0",
            side_effect=SideEffect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
            risk_level=RiskLevel.MEDIUM,
            required_scopes=frozenset({"watchlist:write"}),
            approval=ApprovalMode.ALWAYS,
            egress=Egress.INTERNAL,
            sensitivity=Sensitivity.CONFIDENTIAL,
            retry_profile=RetryProfile.NONE,
            audit_level=AuditLevel.FULL,
            allowed_data_classes=common_data_classes,
        ),
        ToolGovernance(
            tool_id="calculate",
            version="1.0.0",
            side_effect=SideEffect.READ,
            idempotency=Idempotency.IDEMPOTENT,
            risk_level=RiskLevel.LOW,
            required_scopes=frozenset({"tools:read"}),
            approval=ApprovalMode.NONE,
            egress=Egress.NONE,
            sensitivity=Sensitivity.INTERNAL,
            retry_profile=RetryProfile.NONE,
            audit_level=AuditLevel.EXECUTION,
        ),
    )


def mcp_quote_governance() -> ToolGovernance:
    """返回 mcp 工具的治理声明，不创建执行实例。"""
    return ToolGovernance(
        tool_id="get_demo_quote",
        version="1.0.0",
        side_effect=SideEffect.READ,
        idempotency=Idempotency.IDEMPOTENT,
        risk_level=RiskLevel.LOW,
        required_scopes=frozenset({"market:read"}),
        approval=ApprovalMode.NONE,
        egress=Egress.INTERNAL,
        sensitivity=Sensitivity.CONFIDENTIAL,
        retry_profile=RetryProfile.TRANSIENT_READ,
        audit_level=AuditLevel.FULL,
        allowed_data_classes=frozenset(
            {
                DataClassification.PUBLIC,
                DataClassification.INTERNAL,
                DataClassification.CONFIDENTIAL,
            }
        ),
    )


def memory_tool_governance() -> tuple[ToolGovernance, ...]:
    """返回 memory 工具的治理声明，不创建执行实例。"""
    readable_classes = frozenset({DataClassification.INTERNAL, DataClassification.CONFIDENTIAL})
    internal = dict(
        version="1.0.0",
        egress=Egress.INTERNAL,
        sensitivity=Sensitivity.CONFIDENTIAL,
        retry_profile=RetryProfile.NONE,
        audit_level=AuditLevel.FULL,
        allowed_data_classes=readable_classes,
    )
    return (
        ToolGovernance(
            tool_id="search_memories",
            side_effect=SideEffect.READ,
            idempotency=Idempotency.IDEMPOTENT,
            risk_level=RiskLevel.LOW,
            required_scopes=frozenset({"memory:read"}),
            approval=ApprovalMode.NONE,
            **internal,
        ),
        ToolGovernance(
            tool_id="save_memory",
            side_effect=SideEffect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
            risk_level=RiskLevel.MEDIUM,
            required_scopes=frozenset({"memory:write"}),
            approval=ApprovalMode.POLICY,
            **internal,
        ),
        ToolGovernance(
            tool_id="forget_memory",
            side_effect=SideEffect.WRITE,
            idempotency=Idempotency.IDEMPOTENT,
            risk_level=RiskLevel.MEDIUM,
            required_scopes=frozenset({"memory:delete"}),
            approval=ApprovalMode.ALWAYS,
            **internal,
        ),
    )


def ziwei_tool_governance() -> tuple[ToolGovernance, ...]:
    """返回 ziwei 工具的治理声明，不创建执行实例。"""
    return tuple(
        ToolGovernance(
            tool_id=name,
            version="1.0.0",
            side_effect=SideEffect.READ,
            idempotency=Idempotency.IDEMPOTENT,
            risk_level=RiskLevel.LOW,
            required_scopes=frozenset({"ziwei:read"}),
            approval=ApprovalMode.NONE,
            egress=Egress.NONE,
            sensitivity=Sensitivity.CONFIDENTIAL,
            retry_profile=RetryProfile.NONE,
            audit_level=AuditLevel.FULL,
            allowed_data_classes=frozenset({DataClassification.CONFIDENTIAL}),
        )
        for name in ZIWEI_TOOL_INPUTS
    )


def history_tool_governance() -> tuple[ToolGovernance, ...]:
    """历史与工件只读声明，外部工具结果也使用同一回读入口。"""
    return tuple(
        ToolGovernance(
            tool_id=name,
            version="1.0.0",
            side_effect=SideEffect.READ,
            idempotency=Idempotency.IDEMPOTENT,
            risk_level=RiskLevel.LOW,
            required_scopes=frozenset(
                {"artifacts:read" if name == "read_artifact" else "memory:read"}
            ),
            approval=ApprovalMode.NONE,
            egress=Egress.INTERNAL,
            sensitivity=Sensitivity.CONFIDENTIAL,
            retry_profile=RetryProfile.NONE,
            audit_level=AuditLevel.FULL,
        )
        for name in ("search_history", "read_history", "read_artifact")
    )
