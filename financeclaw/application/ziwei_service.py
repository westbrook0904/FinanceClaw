"""紫微跨模块用例：规范化、计算、投影与受保护 Artifact 持久化。"""

import hashlib
import hmac

from financeclaw.kernel import DataClassification, ExecutionContext
from financeclaw.modules.artifacts import ArtifactService
from financeclaw.modules.ziwei.errors import ZiweiError
from financeclaw.modules.ziwei.models import (
    ArtifactReference,
    BirthContext,
    ChartLevel,
    ChartProjection,
    Focus,
    ResolvedTarget,
    ZiweiAnalysisRequest,
)
from financeclaw.modules.ziwei.normalization import birth_context, canonical, resolve_target
from financeclaw.modules.ziwei.service import ZiweiCalculationService, project


class ZiweiService:
    """共享实例只持有服务和配置，不持有当前用户、出生资料或命盘。"""

    def __init__(
        self,
        calculation: ZiweiCalculationService,
        *,
        hmac_key: bytes,
        key_version: str,
        artifacts: ArtifactService | None = None,
        projection_bytes: int = 14_000,
    ) -> None:
        """密钥不进入发布指纹；key version 和策略配置必须进入发布指纹。"""
        if len(hmac_key) < 32 or not key_version:
            raise ValueError(
                "Ziwei requires an explicit HMAC key of at least 32 bytes and key version"
            )
        self.calculation = calculation
        self._hmac_key = hmac_key
        self.key_version = key_version
        self.artifacts = artifacts
        self.projection_bytes = (
            min(projection_bytes, artifacts.inline_bytes - 1024) if artifacts else projection_bytes
        )
        if self.projection_bytes < 1024:
            raise ValueError("Ziwei artifact inline budget is too small")

    def preflight(
        self,
        request: ZiweiAnalysisRequest,
        context: ExecutionContext,
    ) -> tuple[BirthContext, ResolvedTarget | None]:
        """缺失与歧义在任何模型调用前返回，且没有外部网络访问。"""
        self.authorize(context)
        birth = birth_context(
            request,
            context,
            self.calculation.engine,
            self.calculation.convention,
            hmac_key=self._hmac_key,
            key_version=self.key_version,
        )
        return birth, resolve_target(request, context, self.calculation.engine)

    @staticmethod
    def authorize(context: ExecutionContext) -> None:
        """应用用例独立于 Agent 再次鉴权，不能绕过 Tool 调用该服务读资料。"""
        if "ziwei:read" not in context.scopes and "*" not in context.scopes:
            raise PermissionError("ziwei:read is required")

    def calculate(
        self,
        birth: BirthContext,
        target: ResolvedTarget | None,
        level: ChartLevel,
        focus: Focus,
        context: ExecutionContext,
    ) -> ChartProjection:
        """先验证输出预算再持久化确定性快照，执行时钟留在返回 envelope。"""
        self.authorize(context)
        expected = hmac.new(
            self._hmac_key,
            canonical([context.tenant_id, context.subject_id]).encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, birth.owner_fingerprint):
            raise PermissionError("birth context belongs to another owner or HMAC key version")
        result = self.calculation.calculate_snapshot(birth, target, level)
        projection = project(result, focus, max_bytes=self.projection_bytes)
        if self.artifacts:
            metadata = self.artifacts.persist(
                result.model_dump(mode="json", exclude={"target": {"request_clock"}}),
                context=context.model_copy(
                    update={"data_classification": DataClassification.CONFIDENTIAL}
                ),
                source_type="ziwei_chart",
                source_id=result.chart_id,
                idempotency_key=result.chart_id,
            )
            projection = projection.model_copy(
                update={
                    "artifact": ArtifactReference(
                        artifact_id=metadata.artifact_id,
                        content_hash=metadata.content_hash,
                        size_bytes=metadata.size_bytes,
                    )
                }
            )
        if len(projection.model_dump_json().encode()) > self.projection_bytes + 1024:
            raise ZiweiError("ZIWEI_CONTEXT_BUDGET_EXCEEDED", "盘面引用超出输出预算。")
        return projection
