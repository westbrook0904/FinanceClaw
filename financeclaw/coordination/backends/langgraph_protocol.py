"""LangGraph 的协议映射；原生等待位置与消息细节只在 Adapter 内解释。"""

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Self

from pydantic import Field, model_validator

from financeclaw.kernel.coordination import (
    BackendExecutionRef,
    BackendNotification,
    ContinuationRef,
    CoordinationModel,
    DelegationRequest,
    ReleaseRef,
    bounded_digest,
    handoff_input,
)
from financeclaw.kernel.delegation.models import HANDOFF_ADAPTER


class LangGraphContinuationBinding(CoordinationModel):
    """持久保存原 run／checkpoint／interrupt；不能用 thread 最新位置替代。"""

    reference: ContinuationRef
    thread_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    checkpoint: dict[str, Any]
    interrupt_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        """校验不可变绑定摘要，缺少可寻址检查点时明确拒绝。"""
        if not self.checkpoint.get("checkpoint_id"):
            raise ValueError("continuation has no addressable checkpoint")
        if self.reference.source_execution_ref.execution_id != execution_id(
            self.thread_id, self.run_id
        ) or self.reference.binding_hash != bounded_digest(
            self.model_dump(mode="json", exclude={"reference"})
        ):
            raise ValueError("continuation native binding mismatch")
        return self

    def resume_command(self, state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """恢复前复验当前前驱与原 interrupt，缺失时阻塞而不是恢复最新等待点。"""
        LangGraphContinuationBinding.model_validate(self.model_dump())
        if (
            state.get("metadata", {}).get("run_id") != self.run_id
            or state.get("checkpoint") != self.checkpoint
            or self.interrupt_id not in {str(item.get("id")) for item in interrupts(state)}
        ):
            raise ValueError("thread no longer owns the pinned continuation")
        return {"resume": {self.interrupt_id: response}}


def execution_id(thread_id: str, run_id: str) -> str:
    """把原生标识封装为有界 opaque ID；核心不得拆解该字符串。"""
    return json.dumps([thread_id, run_id], separators=(",", ":"))


def interrupts(state: dict[str, Any]) -> list[dict[str, Any]]:
    """只在 Adapter 兼容顶层与 task 内的原生 interrupt。"""
    return list(state.get("interrupts") or []) or [
        item for task in state.get("tasks", []) for item in task.get("interrupts", [])
    ]


def map_delegation(
    *,
    source: BackendExecutionRef,
    root_task_id: str,
    parent_release: ReleaseRef,
    target: ReleaseRef,
    thread_id: str,
    run_id: str,
    state: dict[str, Any],
    interrupt: dict[str, Any],
) -> tuple[DelegationRequest, LangGraphContinuationBinding]:
    """从精确尝试证据包装旧 Handoff；调用方仍须核对树归属、发布及授权。"""
    if (
        state.get("metadata", {}).get("run_id") != run_id
        or source.execution_id != execution_id(thread_id, run_id)
        or interrupt not in interrupts(state)
    ):
        raise ValueError("handoff evidence belongs to another attempt")
    handoff = HANDOFF_ADAPTER.validate_python(interrupt["value"])
    native = {
        "thread_id": thread_id,
        "run_id": run_id,
        "checkpoint": state.get("checkpoint") or {},
        "interrupt_id": interrupt["id"],
    }
    input_hash = bounded_digest(handoff_input(handoff))
    continuation = ContinuationRef(
        continuation_id="continuation:"
        + bounded_digest([source.model_dump(mode="json"), handoff.handoff_id, native]),
        request_id=handoff.handoff_id,
        source_execution_ref=source,
        release=parent_release,
        input_hash=input_hash,
        binding_hash=bounded_digest(native),
    )
    binding = LangGraphContinuationBinding(reference=continuation, **native)
    request = DelegationRequest(
        request_id=handoff.handoff_id,
        root_task_id=root_task_id,
        owner_task_id=source.task_id,
        source_execution_ref=source,
        continuation_ref=continuation,
        input_hash=input_hash,
        handoff=handoff,
        target=target,
    )
    return request, binding


def decode_notification(
    authenticated_body: bytes, *, backend_instance_id: str
) -> BackendNotification:
    """无 I/O 地丢弃 kwargs／values／metadata；通知仅带运行线索和载荷摘要。"""
    if len(authenticated_body) > 65536:
        raise ValueError("webhook body exceeds 64 KiB")
    payload = json.loads(authenticated_body)
    return BackendNotification(
        backend_instance_id=backend_instance_id,
        execution_id=execution_id(str(payload["thread_id"]), str(payload["run_id"])),
        status_hint=payload["status"],
        payload_digest=sha256(authenticated_body).hexdigest(),
        received_at=datetime.now(UTC),
    )
