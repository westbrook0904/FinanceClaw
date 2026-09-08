"""旧 LangGraph 命令的只读解释器；不调用 legacy status、start 或 resume。"""

import json

from financeclaw.coordination.application.releases import release_ref
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.coordination import TaskSubmission, bounded_digest
from financeclaw.shared.execution_ledger.repository import (
    ExecutionConflict,
    digest,
    snapshot_context,
)


class LangGraphLegacyInspector:
    """只转换有完整冻结输入的首次 start；旧 resume 缺应用证明时明确阻塞。"""

    def __init__(self, backend):
        """复用正式 Adapter 的精确操作查找、观察与发布校验。"""
        self.backend = backend

    def map_start(self, execution, operation):
        """保存原 operation ID、输入与发布，拒绝推测遗失命令或前驱。"""
        request, snapshot = operation["request"], execution["snapshot"]
        context = snapshot_context(snapshot)
        if (
            operation["operation_id"] != "operation-" + digest([execution["run_id"], "start"])
            or request.get("command") is not None
            or request.get("predecessor") is not None
            or "kind" in request
        ):
            raise ExecutionConflict("legacy_resume_requires_application_evidence")
        if (
            request.get("thread_id") != snapshot["thread_id"]
            or request.get("assistant_id") != snapshot["assistant_id"]
            or ExecutionContext.model_validate(request.get("context")) != context
            or not isinstance(request.get("input"), dict)
            or digest(request) != operation["request_hash"]
        ):
            raise ExecutionConflict("legacy_command_snapshot_mismatch")
        return TaskSubmission(
            task_id=execution["run_id"],
            root_task_id=execution["root_run_id"],
            operation_id=operation["operation_id"],
            backend_instance_id=self.backend.instance,
            release=release_ref(snapshot),
            input=request["input"],
            input_hash=bounded_digest(request["input"]),
        )

    async def observe(self, command, execution, operation):
        """原操作查找与确切 run/checkpoint 交叉核对；查无结果永不成为重发依据。"""
        receipt = await self.backend.lookup_operation(command)
        if receipt.status != "submitted":
            if (
                operation["status"] == "prepared"
                and operation["server_run_id"] is None
                and execution["server_run_id"] is None
            ):
                return None
            raise ExecutionConflict("legacy_submission_uncertain")
        if operation["status"] == "prepared":
            raise ExecutionConflict("prepared_operation_has_remote_attempt")
        reference = receipt.execution_ref
        native_id = json.loads(reference.execution_id)[1]
        if operation["server_run_id"] not in {None, native_id}:
            raise ExecutionConflict("legacy_attempt_binding_mismatch")
        if execution["server_run_id"] not in {None, native_id}:
            raise ExecutionConflict("legacy_active_position_mismatch")
        observation = await self.backend.observe_execution(reference)
        if observation.status not in {"waiting", "completed", "failed"}:
            raise ExecutionConflict("legacy_backend_not_quiescent")
        return observation

    def verify_delegation(self, request, binding, record, child):
        """旧父等待与已分配 child 的原生 ID 必须与 shadow 同位，不重新解析资料引用。"""
        saved = record["execution_snapshot"]
        native_run = json.loads(request.source_execution_ref.execution_id)[1]
        if (
            saved.get("parent_server_run_id") != native_run
            or saved.get("parent_interrupt_id") not in {None, binding["interrupt_id"]}
            or record["child_thread_id"] != child["snapshot"]["thread_id"]
            or record["child_server_run_id"] not in {None, child["server_run_id"]}
            or (
                record["kind"] == "agent"
                and saved.get("resolved_context", [])
                != child["snapshot"].get("resolved_context", [])
            )
        ):
            raise ExecutionConflict("legacy_delegation_position_mismatch")
