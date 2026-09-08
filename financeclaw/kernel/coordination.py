"""Stage-8 协调边界：稳定请求、精确尝试与响应证据；不携带运行时原生对象。"""

import json
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Literal, Self

from jsonschema import Draft202012Validator
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from financeclaw.kernel.delegation.models import (
    AgentHandoffV2,
    DelegationResult,
    HandoffRequest,
    WorkflowHandoff,
)
from financeclaw.kernel.interactions import InteractionPoint, InteractionResponse

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def bounded_digest(value: JsonValue) -> str:
    """为有界 JSON 固定摘要；拒绝 NaN 和超过 16 KiB 的内联输入。"""
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(encoded) > 16384:
        raise ValueError("coordination input exceeds 16 KiB")
    return sha256(encoded).hexdigest()


def handoff_input(handoff: HandoffRequest) -> dict[str, JsonValue]:
    """复用原 Handoff 的权威参数及摘要口径，不维护另一份委派输入。"""
    if isinstance(handoff, WorkflowHandoff):
        return handoff.arguments
    value = {"task": handoff.task, "context_refs": list(handoff.context_refs)}
    if isinstance(handoff, AgentHandoffV2):
        value["arguments"] = handoff.arguments
    return value


class CoordinationModel(BaseModel):
    """版本化边界模型；入库和出站时须重新校验，frozen 不代表嵌套字典不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ReleaseRef(CoordinationModel):
    """可信发布配置解析出的逻辑目标，禁止恢复时重新解析 latest。"""

    kind: Literal["agent", "workflow"]
    target_id: Identifier
    version: Annotated[str, Field(min_length=1, max_length=32)]
    fingerprint: Digest


class BackendExecutionRef(CoordinationModel):
    """一个业务 run 的一次 backend 尝试；execution_id 仅由对应 Adapter 解释。"""

    backend_instance_id: Identifier
    task_id: Identifier
    operation_id: Identifier
    execution_id: Annotated[str, Field(min_length=1, max_length=2048)]


class ContinuationRef(CoordinationModel):
    """原请求的持久等待位置；绑定内容由 Adapter 保存，核心只核对归属和摘要。"""

    schema_version: Literal[1] = 1
    continuation_id: Identifier
    request_id: Identifier
    source_execution_ref: BackendExecutionRef
    release: ReleaseRef
    input_hash: Digest
    binding_hash: Digest


class BackendNotification(CoordinationModel):
    """已认证通知的最小线索；没有 tenant、授权、输入、结果或完成决定。"""

    schema_version: Literal[1] = 1
    backend_instance_id: Identifier
    execution_id: Annotated[str, Field(min_length=1, max_length=2048)]
    event_id: Identifier | None = None
    status_hint: Annotated[str, Field(min_length=1, max_length=32)]
    payload_digest: Digest
    received_at: AwareDatetime


class RequestBinding(CoordinationModel):
    """一次协调请求的共同归属；身份和 release 来自可信观察与既有执行快照。"""

    schema_version: Literal[1] = 1
    request_id: Identifier
    root_task_id: Identifier
    owner_task_id: Identifier
    source_execution_ref: BackendExecutionRef
    continuation_ref: ContinuationRef
    input_hash: Digest

    @model_validator(mode="after")
    def validate_owner(self) -> Self:
        """通知、旧尝试、另一个请求或输入都不能替换原 continuation。"""
        continuation = self.continuation_ref
        if (
            self.source_execution_ref.task_id != self.owner_task_id
            or continuation.source_execution_ref != self.source_execution_ref
            or continuation.request_id != self.request_id
            or continuation.input_hash != self.input_hash
        ):
            raise ValueError("request does not own its continuation")
        return self


class DelegationRequest(RequestBinding):
    """包装已有 Handoff；target 是复验后的 child 发布，continuation 固定 parent 发布。"""

    kind: Literal["delegation"] = "delegation"
    handoff: HandoffRequest
    target: ReleaseRef
    result_contract_ref: Literal["delegation-result.v1"] = "delegation-result.v1"

    @model_validator(mode="after")
    def validate_handoff(self) -> Self:
        """拒绝稳定 ID 被复用于另一父任务、目标、版本或参数。"""
        handoff = self.handoff
        target_id = (
            handoff.workflow_id if isinstance(handoff, WorkflowHandoff) else handoff.agent_id
        )
        if (
            handoff.handoff_id != self.request_id
            or handoff.parent_run_id != self.owner_task_id
            or handoff.kind != self.target.kind
            or target_id != self.target.target_id
            or getattr(handoff, "target_version", None) not in (None, self.target.version)
            or bounded_digest(handoff_input(handoff)) != self.input_hash
        ):
            raise ValueError("handoff binding or input hash mismatch")
        return self


class InteractionRequest(RequestBinding):
    """资料、选择和审批使用发布的声明，不把自然语言问题猜成用户交互。"""

    kind: Literal["interaction"] = "interaction"
    point: InteractionPoint
    revision: int = Field(ge=1)
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    expires_at: AwareDatetime
    action_hash: Digest | None = None

    @model_validator(mode="after")
    def validate_question(self) -> Self:
        """冻结展示、声明、版本和审批动作的摘要。"""
        if (self.point.kind == "approval") != (self.action_hash is not None):
            raise ValueError("only approval interactions require an action hash")
        payload = self.model_dump(
            mode="json", include={"point", "revision", "question", "expires_at", "action_hash"}
        )
        if bounded_digest(payload) != self.input_hash:
            raise ValueError("interaction input hash mismatch")
        return self


CoordinationRequest = Annotated[DelegationRequest | InteractionRequest, Field(discriminator="kind")]


class ResponseDelivery(CoordinationModel):
    """一次固定响应命令；HTTP 接受只到 submitted，应用确认另需精确证据。"""

    operation_id: Identifier
    request: CoordinationRequest
    response: DelegationResult | InteractionResponse
    responding_task_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_response(self) -> Self:
        """响应种类、原请求、child 和输入都必须匹配；决定本身不提供授权。"""
        request, response = self.request, self.response
        if isinstance(request, DelegationRequest):
            if not isinstance(response, DelegationResult) or (
                response.delegation_id != request.request_id
                or response.parent_run_id != request.owner_task_id
                or response.arguments_hash != request.input_hash
                or response.kind != request.target.kind
                or response.target_id != request.target.target_id
                or response.target_version != request.target.version
                or response.child_run_id != self.responding_task_id
            ):
                raise ValueError("delegation result does not match the pinned request")
        elif not isinstance(response, InteractionResponse) or (
            response.kind != request.point.kind
            or response.revision != request.revision
            or response.action_hash != request.action_hash
            or self.responding_task_id is not None
        ):
            raise ValueError("interaction response does not match the pinned request")
        elif request.point.kind == "input":
            if not Draft202012Validator(request.point.response_schema).is_valid(response.answer):
                raise ValueError("answer does not match the published input schema")
        elif request.point.kind == "choice" and response.answer not in request.point.options:
            raise ValueError("answer is not a published choice")
        bounded_digest(response.model_dump(mode="json"))
        return self


class SubmissionReceipt(CoordinationModel):
    """查不到回执仍是不确定；不表达可重新提交的 not-executed 状态。"""

    status: Literal["submitted", "uncertain"]
    execution_ref: BackendExecutionRef | None = None

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        """只有 submitted 才包含已经查明的确切尝试。"""
        if (self.status == "submitted") != (self.execution_ref is not None):
            raise ValueError("submission receipt must match its certainty")
        return self


class ResponseApplicationEvidence(CoordinationModel):
    """原恢复尝试中应用响应的证据；子任务成功与父任务执行终态相互独立。"""

    operation_id: Identifier
    request_id: Identifier
    continuation_id: Identifier
    execution_ref: BackendExecutionRef
    response_hash: Digest
    checkpoint_ref: Annotated[str, Field(min_length=1, max_length=2048)]

    def confirms(self, command: ResponseDelivery, resumed: BackendExecutionRef) -> bool:
        """不能拿另一恢复尝试的 ToolMessage 或相同 thread 的最终值确认交付。"""
        original = command.request.source_execution_ref
        return (
            self.operation_id == command.operation_id == resumed.operation_id
            and self.request_id == command.request.request_id
            and self.continuation_id == command.request.continuation_ref.continuation_id
            and self.execution_ref == resumed
            and resumed.task_id == original.task_id
            and resumed.backend_instance_id == original.backend_instance_id
            and self.response_hash == bounded_digest(command.response.model_dump(mode="json"))
        )


class BackendObservation(CoordinationModel):
    """精确尝试的观察；结果仅为 JSON，核心不接收 LangChain 消息类。"""

    execution_ref: BackendExecutionRef
    status: Literal["active", "waiting", "completed", "failed", "cancelled", "unknown"]
    requests: tuple[CoordinationRequest, ...] = ()
    result: JsonValue = None
    response_applications: tuple[ResponseApplicationEvidence, ...] = ()
    evidence_ref: Annotated[str, Field(min_length=1, max_length=2048)] | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        """拒绝混入旧尝试的请求，完成必须具备可定位的证据引用。"""
        if any(request.source_execution_ref != self.execution_ref for request in self.requests):
            raise ValueError("observation contains requests owned by another attempt")
        if self.requests and self.status != "waiting":
            raise ValueError("coordination requests require a waiting observation")
        if any(proof.execution_ref != self.execution_ref for proof in self.response_applications):
            raise ValueError("response evidence belongs to another execution")
        if self.status == "completed" and self.evidence_ref is None:
            raise ValueError("completed observation requires exact execution evidence")
        return self


class BackendCapabilities(CoordinationModel):
    """经过部署探针验证的静态能力记录，不接受 backend 自报能力作为授权。"""

    exact_observation: bool
    durable_continuation: bool
    recoverable_requests: bool
    operation_lookup: bool
    submission_idempotent: bool = False
    cancellation_confirmation: bool
    response_application_evidence: bool
    webhook_statuses: frozenset[str] = frozenset()

    def require_role(self, role: Literal["parent", "child"]) -> None:
        """一次性 backend 可作为 child；无法定位等待位置时拒绝作为 parent。"""
        if not self.exact_observation or not (self.operation_lookup or self.submission_idempotent):
            raise ValueError("backend cannot safely identify submitted executions")
        if not self.cancellation_confirmation:
            raise ValueError("backend cannot confirm cancellation")
        if role == "parent" and not (
            self.durable_continuation
            and self.recoverable_requests
            and self.response_application_evidence
        ):
            raise ValueError("backend cannot safely continue a delegating parent")


class TaskSubmission(CoordinationModel):
    """Worker 出站的冻结输入；backend 实例与 release 由配置固定，无远程地址。"""

    task_id: Identifier
    root_task_id: Identifier
    operation_id: Identifier
    backend_instance_id: Identifier
    release: ReleaseRef
    input: JsonValue
    input_hash: Digest

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        """每次出站都重验原输入摘要。"""
        if bounded_digest(self.input) != self.input_hash:
            raise ValueError("submission input hash mismatch")
        return self


class CancellationReceipt(CoordinationModel):
    """请求接受与确切停止分开；不把 HTTP 200 当作已取消。"""

    execution_ref: BackendExecutionRef
    status: Literal["requested", "confirmed", "uncertain", "unsupported"]
    observed_at: datetime | None = None
