"""错误绑定、跨 backend、能力不足与数据漂移的反例测试。"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from financeclaw.coordination.backends.langgraph_protocol import (
    decode_notification,
    execution_id,
    map_delegation,
)
from financeclaw.kernel.coordination import (
    BackendCapabilities,
    BackendExecutionRef,
    BackendObservation,
    CoordinationRequest,
    ReleaseRef,
    ResponseApplicationEvidence,
    ResponseDelivery,
    SubmissionReceipt,
    TaskSubmission,
    bounded_digest,
)
from financeclaw.kernel.delegation.models import AgentHandoffV2, DelegationResult


@pytest.fixture
def bound_request():
    """可信状态与旧 Handoff 组成一个可验证的显式委派请求。"""
    release = ReleaseRef(kind="agent", target_id="parent", version="1.0.0", fingerprint="a" * 64)
    child = ReleaseRef(kind="agent", target_id="child", version="2.0.0", fingerprint="b" * 64)
    source = BackendExecutionRef(
        backend_instance_id="parent-backend",
        task_id="parent",
        operation_id="original-operation",
        execution_id=execution_id("thread", "run"),
    )
    handoff = AgentHandoffV2(
        handoff_id="handoff",
        parent_run_id="parent",
        parent_turn_id="turn",
        conversation_id="conversation",
        agent_id="child",
        target_version="2.0.0",
        task="bounded task",
        arguments={"symbols": ["AAPL"]},
    )
    interrupt = {"id": "interrupt", "value": handoff.model_dump(mode="json")}
    state = {
        "metadata": {"run_id": "run"},
        "checkpoint": {"checkpoint_id": "checkpoint"},
        "tasks": [{"interrupts": [interrupt]}],
    }
    request, binding = map_delegation(
        source=source,
        root_task_id="root",
        parent_release=release,
        target=child,
        thread_id="thread",
        run_id="run",
        state=state,
        interrupt=interrupt,
    )
    return request, binding, state


def delivery(request) -> ResponseDelivery:
    """响应明确带 child、原 parent、输入摘要与版本。"""
    return ResponseDelivery(
        operation_id="delivery:handoff",
        request=request,
        responding_task_id="child-task",
        response=DelegationResult(
            delegation_id=request.request_id,
            kind="agent",
            target_id="child",
            target_version="2.0.0",
            child_run_id="child-task",
            parent_run_id="parent",
            arguments_hash=request.input_hash,
            status="completed",
            output={"value": 1},
        ),
    )


def test_explicit_handoff_round_trip_retains_single_authoritative_input(bound_request) -> None:
    """旧 Handoff 无损映射，父 backend 的 continuation 与 child 目标相互独立。"""
    request, binding, state = bound_request
    restored = TypeAdapter(CoordinationRequest).validate_json(request.model_dump_json())
    assert restored == request
    assert request.handoff.arguments == {"symbols": ["AAPL"]}
    assert request.continuation_ref.release.target_id == "parent"
    assert request.target.target_id == "child"
    assert binding.resume_command(state, delivery(request).response.model_dump()) == {
        "resume": {"interrupt": delivery(request).response.model_dump()}
    }


@pytest.mark.parametrize("mutation", ["backend", "attempt", "request", "hash", "checkpoint"])
def test_stale_or_tampered_continuations_are_rejected(bound_request, mutation) -> None:
    """同 thread 最新 checkpoint 及伪造的输入、请求和部署归属都不能替代原位置。"""
    request, binding, state = bound_request
    raw = request.model_dump(mode="json")
    if mutation == "checkpoint":
        state["checkpoint"] = {"checkpoint_id": "newer-checkpoint"}
        with pytest.raises(ValueError, match="no longer owns"):
            binding.resume_command(state, {})
        return
    if mutation == "backend":
        raw["source_execution_ref"]["backend_instance_id"] = "another-backend"
    elif mutation == "attempt":
        raw["source_execution_ref"]["operation_id"] = "another-resume"
    elif mutation == "request":
        raw["request_id"] = "another-request"
    else:
        raw["handoff"]["arguments"] = {"symbols": ["MSFT"]}
    with pytest.raises(ValidationError):
        TypeAdapter(CoordinationRequest).validate_python(raw)


def test_unaddressable_or_unrelated_checkpoint_is_rejected(bound_request) -> None:
    """适配器不能因为 callback 带原 run ID 就接纳另一尝试的状态值。"""
    request, _, state = bound_request
    for bad in ({}, {**state, "metadata": {"run_id": "later"}}):
        with pytest.raises((ValueError, KeyError)):
            map_delegation(
                source=request.source_execution_ref,
                root_task_id="root",
                parent_release=request.continuation_ref.release,
                target=request.target,
                thread_id="thread",
                run_id="run",
                state=bad,
                interrupt=state["tasks"][0]["interrupts"][0],
            )


@pytest.mark.parametrize(
    "field,value",
    [
        ("delegation_id", "other"),
        ("parent_run_id", "other"),
        ("child_run_id", "other"),
        ("target_version", "latest"),
        ("arguments_hash", "f" * 64),
        ("target_id", "other"),
    ],
)
def test_result_cannot_cross_request_release_or_child(bound_request, field, value) -> None:
    """Child 终态并不授权将结果交给任何等待中的 parent。"""
    raw = delivery(bound_request[0]).model_dump(mode="json")
    raw["response"][field] = value
    with pytest.raises(ValidationError):
        ResponseDelivery.model_validate(raw)


def test_submitted_is_not_response_applied(bound_request) -> None:
    """HTTP 回执、当前尝试和已应用响应分别验证。"""
    command = delivery(bound_request[0])
    resumed = BackendExecutionRef(
        backend_instance_id="parent-backend",
        task_id="parent",
        operation_id=command.operation_id,
        execution_id="new-resume",
    )
    receipt = SubmissionReceipt(status="submitted", execution_ref=resumed)
    assert not hasattr(receipt, "applied")
    proof = ResponseApplicationEvidence(
        operation_id=command.operation_id,
        request_id=command.request.request_id,
        continuation_id=command.request.continuation_ref.continuation_id,
        execution_ref=resumed,
        response_hash=bounded_digest(command.response.model_dump()),
        checkpoint_ref="resumed-checkpoint",
    )
    assert proof.confirms(command, resumed)
    observation = BackendObservation(
        execution_ref=resumed,
        status="failed",
        response_applications=(proof,),
        evidence_ref="parent-failed-after-response",
    )
    assert observation.response_applications[0].confirms(command, resumed)
    # Parent failure after applying a response must remain failure; application is separate.
    assert observation.status == "failed"
    with pytest.raises(ValidationError, match="another execution"):
        BackendObservation(
            execution_ref=resumed.model_copy(update={"execution_id": "other"}),
            status="failed",
            response_applications=(proof,),
        )
    assert not proof.model_copy(update={"continuation_id": "new-wait"}).confirms(command, resumed)
    assert not proof.confirms(command, resumed.model_copy(update={"operation_id": "other"}))


def test_notification_is_minimal_and_has_no_authority() -> None:
    """即使已认证 backend 带值和身份，Ingress 归一化结果也没有这些授权字段。"""
    body = json.dumps(
        {
            "run_id": "native",
            "thread_id": "thread",
            "status": "success",
            "tenant_id": "forged",
            "kwargs": {"input": "private"},
            "values": {"credential": "private"},
            "metadata": {"scopes": ["*"]},
        }
    ).encode()
    notification = decode_notification(body, backend_instance_id="trusted-deployment")
    value = notification.model_dump(mode="json")
    assert value["backend_instance_id"] == "trusted-deployment"
    assert set(value).isdisjoint({"tenant_id", "scopes", "kwargs", "values", "metadata"})
    assert "private" not in notification.model_dump_json()
    assert notification.event_id is None
    assert notification.received_at <= datetime.now(UTC)
    with pytest.raises(ValueError, match="64 KiB"):
        decode_notification(b" " * 65537, backend_instance_id="trusted")


def test_limited_backend_can_be_child_but_not_parent() -> None:
    """扩展预留通过能力门禁；不是宣传第二种生产 backend 已上线。"""
    capabilities = BackendCapabilities(
        exact_observation=True,
        durable_continuation=False,
        recoverable_requests=False,
        operation_lookup=True,
        cancellation_confirmation=True,
        response_application_evidence=False,
    )
    capabilities.require_role("child")
    with pytest.raises(ValueError, match="parent"):
        capabilities.require_role("parent")
    with pytest.raises(ValueError, match="identify"):
        capabilities.model_copy(update={"operation_lookup": False}).require_role("child")


def test_unknown_receipt_and_incomplete_observation_cannot_claim_success(bound_request) -> None:
    """Not found 不是 not executed，缺少可定位证据不构成完成。"""
    assert SubmissionReceipt(status="uncertain").execution_ref is None
    with pytest.raises(ValidationError):
        SubmissionReceipt(status="not_executed")
    with pytest.raises(ValidationError):
        BackendObservation(execution_ref=bound_request[0].source_execution_ref, status="completed")


def test_input_is_bounded_and_revalidated_before_dispatch(bound_request) -> None:
    """Pydantic frozen 不冻结嵌套字典，出站重验能发现准备后被原地改写的输入。"""
    command = TaskSubmission(
        task_id="task",
        root_task_id="task",
        operation_id="start",
        backend_instance_id="child-backend",
        release=bound_request[0].target,
        input={"x": 1},
        input_hash=bounded_digest({"x": 1}),
    )
    command.input["x"] = 2
    with pytest.raises(ValidationError):
        TaskSubmission.model_validate(command.model_dump())
    with pytest.raises(ValueError):
        bounded_digest({"x": float("nan")})
    with pytest.raises(ValueError, match="16 KiB"):
        bounded_digest("大" * 6000)
