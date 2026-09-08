"""Coordination 的有界业务推进；持久化责任负责重唤醒，业务操作日志负责副作用。"""

from typing import Any

from experiments.stage8.backend import ProbeBackend, release
from experiments.stage8.store import ProbeStore, now
from financeclaw.kernel.coordination import (
    BackendExecutionRef,
    DelegationRequest,
    InteractionRequest,
    ResponseDelivery,
    TaskSubmission,
    bounded_digest,
)
from financeclaw.kernel.delegation.models import DelegationResult
from financeclaw.kernel.interactions import InteractionResponse

TERMINAL = {"completed", "failed", "cancelled", "authorization_expired"}


class LostReceipt(RuntimeError):
    """远端真实执行后丢掉本地回执；再次进入只能恢复原操作。"""


class ProbeDriver:
    """可重入的一步协调；远程 HTTP 总在数据库事务之外。"""

    def __init__(self, store: ProbeStore, backend: ProbeBackend) -> None:
        self.store, self.backend = store, backend

    async def advance(self, root_id: str, claim: dict[str, Any] | None = None) -> float | None:
        """推进一个稳定前驱，返回下一次核对秒数；None 表示停止实验责任。"""
        before = self.store.read(root_id)
        payload = before["payload"]
        stage = payload["stage"]
        if stage in TERMINAL:
            return None
        if before["cancelled"] or before["grant_until"] <= now():
            for execution in self.store.execution.tree(root_id):
                for operation in self.store.execution.operations_for_run(execution["run_id"]):
                    if operation["status"] == "prepared":
                        continue
                    raw = operation["request"]
                    command = (
                        ResponseDelivery.model_validate(raw)
                        if "request" in raw
                        else TaskSubmission.model_validate(raw)
                    )
                    reference = await self.backend.lookup(command)
                    if reference is None:
                        if payload.get("blocked_reason") != "submission_unresolved":
                            self.store.update(
                                before,
                                {**payload, "blocked_reason": "submission_unresolved"},
                                claim,
                            )
                        return 5
                    if not await self.backend.cancel(reference):
                        return 0.2
            stage = "cancelled" if before["cancelled"] else "authorization_expired"
            self.store.update(before, {**payload, "stage": stage}, claim)
            return None
        if stage.endswith("_start") or stage.endswith("_resume"):
            return await self._submit(before, claim)
        if stage == "user_wait":
            if "answer" not in payload:
                return max(0.05, (before["grant_until"] - now()).total_seconds())
            request = InteractionRequest.model_validate(payload["interaction"])
            command = ResponseDelivery(
                operation_id="answer:" + request.request_id,
                request=request,
                response=InteractionResponse(
                    revision=request.revision, kind=request.point.kind, answer=payload["answer"]
                ),
            )
            self.store.update(
                before,
                {**payload, "stage": "child_resume", "command": command.model_dump(mode="json")},
                claim,
            )
            return 0.01
        reference = BackendExecutionRef.model_validate(payload["reference"])
        observed = await self.backend.observe(reference)
        if observed.status in {"active", "unknown"}:
            return 0.2
        if observed.status == "failed":
            self.store.update(before, {**payload, "stage": "failed"}, claim)
            return None
        if stage == "root_observe":
            request = DelegationRequest.model_validate(observed.requests[0])
            child_id = "child:" + bounded_digest(request.request_id)
            child_input = {"task_id": child_id, "request_id": "interaction:" + root_id}
            command = TaskSubmission(
                task_id=child_id,
                root_task_id=root_id,
                operation_id="start:" + child_id,
                backend_instance_id="probe",
                release=release("child"),
                input=child_input,
                input_hash=bounded_digest(child_input),
            )
            self.store.update(
                before,
                {
                    **payload,
                    "stage": "child_start",
                    "delegation": request.model_dump(mode="json"),
                    "child_id": child_id,
                    "parent_reference": reference.model_dump(mode="json"),
                    "command": command.model_dump(mode="json"),
                },
                claim,
            )
        elif stage == "child_observe":
            self.store.update(
                before,
                {
                    **payload,
                    "stage": "user_wait",
                    "interaction": observed.requests[0].model_dump(mode="json"),
                },
                claim,
            )
        elif stage == "child_resume_observe":
            if observed.status != "completed":
                raise ValueError("unexpected probe child continuation")
            request = DelegationRequest.model_validate(payload["delegation"])
            result = DelegationResult(
                delegation_id=request.request_id,
                kind=request.target.kind,
                target_id=request.target.target_id,
                target_version=request.target.version,
                child_run_id=payload["child_id"],
                parent_run_id=root_id,
                arguments_hash=request.input_hash,
                status="completed",
                output=observed.result,
            )
            command = ResponseDelivery(
                operation_id="delivery:" + request.request_id,
                request=request,
                response=result,
                responding_task_id=payload["child_id"],
            )
            self.store.update(
                before,
                {
                    **payload,
                    "stage": "root_resume",
                    "child_status": "completed",
                    "delivery_status": "pending",
                    "command": command.model_dump(mode="json"),
                },
                claim,
            )
        elif stage == "root_resume_observe":
            command = ResponseDelivery.model_validate(payload["command"])
            evidence = await self.backend.application_evidence(command, reference)
            if evidence is None or not evidence.confirms(command, reference):
                return 0.2
            self.store.update(
                before,
                {
                    **payload,
                    "stage": "completed",
                    "delivery_status": "applied",
                    "evidence": evidence.model_dump(mode="json"),
                },
                claim,
                final={
                    "operation_id": reference.operation_id,
                    "execution_id": reference.execution_id,
                    "result": observed.model_dump(mode="json"),
                },
            )
            return None
        else:
            raise ValueError("unknown probe stage: " + stage)
        return 0.01

    async def _submit(self, before: dict[str, Any], claim: dict[str, Any] | None) -> float:
        """领取和远程提交分开；回执丢失后的 worker 重入不重发。"""
        root_id, payload = before["run_id"], before["payload"]
        raw = payload["command"]
        command = (
            ResponseDelivery.model_validate(raw)
            if "request" in raw
            else TaskSubmission.model_validate(raw)
        )
        task_id = (
            command.request.owner_task_id
            if isinstance(command, ResponseDelivery)
            else command.task_id
        )
        claimed = self.store.command_claim(root_id, raw, task_id, claim)
        if claimed:
            reference = (
                await self.backend.deliver(command)
                if isinstance(command, ResponseDelivery)
                else await self.backend.submit(command)
            )
            if payload.get("lose_receipt"):
                self.store.receipt(root_id, command.operation_id, None, claim)
                raise LostReceipt("remote success, local receipt lost")
        else:
            reference = await self.backend.lookup(command)
            if reference is None:
                return 0.2
        self.store.receipt(root_id, command.operation_id, reference.execution_id, claim)
        stage = payload["stage"]
        observed_stage = (
            stage.removesuffix("_start") + "_observe"
            if stage.endswith("_start")
            else stage + "_observe"
        )
        self.store.update(
            before,
            {**payload, "stage": observed_stage, "reference": reference.model_dump(mode="json")},
            claim,
        )
        return 0.01
