"""API 与 Worker 共用的太卜静态发布，导入时不连接远端或加载执行端。"""

import json
from hashlib import sha256
from importlib.resources import files

from financeclaw.kernel.context import DataClassification
from financeclaw.kernel.taibu import TAIBU_TOOL_INPUTS, TaibuToolResult
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
from financeclaw.shared.infrastructure.settings import FinanceClawSettings

TAIBU_SERVER_VERSION = "3.1.1"
TAIBU_SOURCE_COMMIT = "e8f636972a6fdb14f2a532ee223101f889ab4820"


def remote_contracts() -> dict:
    """每次返回独立数据，防止一个运行修改其他运行的契约。"""
    return json.loads(files(__package__).joinpath("taibu_contracts.json").read_text("utf-8"))


def contract_hash(contract: dict) -> str:
    """规范化 MCP 契约，名称、输入、输出与注解均参与摘要。"""
    value = {
        key: contract.get(key) for key in ("name", "inputSchema", "outputSchema", "annotations")
    }
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def taibu_governance(settings: FinanceClawSettings) -> tuple[ToolGovernance, ...]:
    """仅发布配置允许的工具，出生资料只走明确的内网部署。"""
    if not settings.taibu_enabled:
        return ()
    external = settings.taibu_egress == "external"
    return tuple(
        ToolGovernance(
            tool_id=f"taibu_{name}",
            version="1.0.0",
            side_effect=SideEffect.READ,
            idempotency=Idempotency.IDEMPOTENT,
            risk_level=RiskLevel.LOW,
            required_scopes=frozenset({"taibu:read"}),
            approval=ApprovalMode.NONE,
            egress=Egress.EXTERNAL if external else Egress.INTERNAL,
            sensitivity=Sensitivity.CONFIDENTIAL if name == "bazi" else Sensitivity.INTERNAL,
            retry_profile=RetryProfile.TRANSIENT_READ,
            audit_level=AuditLevel.FULL,
            tenant_allowlist=settings.taibu_tenant_allowlist,
            allowed_data_classes=frozenset({DataClassification.CONFIDENTIAL})
            if name == "bazi"
            else frozenset(
                {DataClassification.PUBLIC, DataClassification.INTERNAL}
                | (set() if external else {DataClassification.CONFIDENTIAL})
            ),
        )
        for name in sorted(settings.taibu_allowed_tools)
    )


def taibu_release(settings: FinanceClawSettings) -> dict:
    """冻结所有影响执行行为的非密钥配置与 Schema。"""
    contracts = remote_contracts()
    return {
        "source_commit": TAIBU_SOURCE_COMMIT,
        "server_version": TAIBU_SERVER_VERSION,
        "url": settings.taibu_mcp_url,
        "egress": settings.taibu_egress,
        "allowed_hosts": sorted(settings.taibu_allowed_hosts),
        "timeout_seconds": settings.taibu_timeout_seconds,
        "projection_bytes": settings.taibu_projection_bytes,
        "result_max_bytes": settings.taibu_result_max_bytes,
        "contract_cache_seconds": settings.taibu_contract_cache_seconds,
        "result_schema": TaibuToolResult.model_json_schema(),
        "tools": {
            name: {
                "remote": contracts[name],
                "local": TAIBU_TOOL_INPUTS[f"taibu_{name}"].model_json_schema(),
            }
            for name in sorted(settings.taibu_allowed_tools)
        },
    }
