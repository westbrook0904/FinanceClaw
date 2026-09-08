"""正式 LangGraph Backend Adapter：原生协议、回调和检查点细节止于此处。"""

import json
from datetime import datetime, timedelta

from langgraph_sdk.errors import NotFoundError
from sqlalchemy import func, select

from financeclaw.coordination.application.releases import release_ref
from financeclaw.coordination.application.run_observation import observe_run
from financeclaw.coordination.application.streaming import final_assistant_content
from financeclaw.coordination.backends.langgraph import LangGraphAgentServerClient
from financeclaw.coordination.backends.langgraph_protocol import (
    LangGraphContinuationBinding,
    decode_notification,
    execution_id,
    interrupts,
    map_delegation,
)
from financeclaw.kernel.coordination import (
    BackendCapabilities,
    BackendExecutionRef,
    BackendObservation,
    CancellationReceipt,
    ContinuationRef,
    DelegationRequest,
    InteractionRequest,
    ResponseApplicationEvidence,
    ResponseDelivery,
    SubmissionReceipt,
    TaskSubmission,
    bounded_digest,
)
from financeclaw.kernel.interactions import InteractionPoint
from financeclaw.shared.execution_ledger.coordination_tables import ContinuationRow
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.repository import (
    ExecutionConflict,
    digest,
    snapshot_context,
)


class LangGraphBackend:
    """生产仅使用此 Adapter；服务地址、回调和发布绑定来自受信任配置。"""

    capabilities = BackendCapabilities(
        exact_observation=True,
        durable_continuation=True,
        recoverable_requests=True,
        operation_lookup=True,
        cancellation_confirmation=True,
        response_application_evidence=True,
        webhook_statuses=frozenset({"success", "error"}),
    )

    def __init__(self, settings, repository, releases, *, native=None):
        """可注入真实 HTTP 客户端；不在 BFF 中发起执行。"""
        self.settings, self.repository, self.releases = settings, repository, releases
        self.native = native or LangGraphAgentServerClient(
            url=settings.agent_server_url,
            service_token=settings.agent_server_service_token.get_secret_value()
            if settings.agent_server_service_token
            else None,
            timeout_seconds=settings.agent_server_timeout_seconds,
        )
        self.client = self.native._client
        self.instance = settings.coordinator_backend_instance_id

    def decode_notification(self, authenticated_body):
        """仅解析已认证的最小通知，不在回调处理栈内读取 backend。"""
        return decode_notification(authenticated_body, backend_instance_id=self.instance)

    def _command(self, command):
        """核对持久化命令、身份及固定发布；运行上下文不来自模型或回调。"""
        task_id = (
            command.task_id
            if isinstance(command, TaskSubmission)
            else command.request.owner_task_id
        )
        operation = self.repository.execution.operation(command.operation_id)
        snapshot = self.repository.execution.get(task_id)["snapshot"]
        if (
            operation["request"]["payload"] != command.model_dump(mode="json")
            or operation["status"] not in {"claimed", "uncertain"}
            or snapshot.get("backend_instance_id") != self.instance
        ):
            raise ExecutionConflict("backend command is not the claimed immutable operation")
        self.releases.verify(snapshot)
        expected = (
            command.release
            if isinstance(command, TaskSubmission)
            else command.request.continuation_ref.release
        )
        if release_ref(snapshot) != expected:
            raise ExecutionConflict("backend command release mismatch")
        context = snapshot_context(snapshot, frozenset(operation["request"]["scopes"]))
        self.repository.execution.verify_context(context)
        return snapshot, context

    def _reference(self, task_id, operation_id, thread_id, native_id):
        """原生 ID 只由 Adapter 编解码。"""
        return BackendExecutionRef(
            backend_instance_id=self.instance,
            task_id=task_id,
            operation_id=operation_id,
            execution_id=execution_id(thread_id, str(native_id)),
        )

    async def submit_task(self, command: TaskSubmission) -> SubmissionReceipt:
        """唯一操作领取后提交一次；HTTP 断连由 Worker 保留 uncertain。"""
        command = TaskSubmission.model_validate(command.model_dump())
        snapshot, context = self._command(command)
        await self.native.create_thread(snapshot["thread_id"])
        run = await self.client.runs.create(
            snapshot["thread_id"],
            snapshot["assistant_id"],
            input=command.input,
            context=context.model_dump(mode="json"),
            metadata={
                **context.trace_metadata(),
                "operation_id": command.operation_id,
                "application_run_id": command.task_id,
            },
            webhook=self.settings.coordinator_callback_url,
            multitask_strategy="reject",
        )
        return SubmissionReceipt(
            status="submitted",
            execution_ref=self._reference(
                command.task_id, command.operation_id, snapshot["thread_id"], run["run_id"]
            ),
        )

    async def lookup_operation(self, command) -> SubmissionReceipt:
        """即使授权已过期也允许查回执，查不到不意味着远端未提交。"""
        task_id = (
            command.task_id
            if isinstance(command, TaskSubmission)
            else command.request.owner_task_id
        )
        snapshot = self.repository.execution.get(task_id)["snapshot"]
        try:
            found = await self.native.find_operation(
                thread_id=snapshot["thread_id"], operation_id=command.operation_id
            )
        except NotFoundError:
            found = None
        return (
            SubmissionReceipt(status="uncertain")
            if found is None
            else SubmissionReceipt(
                status="submitted",
                execution_ref=self._reference(
                    task_id, command.operation_id, snapshot["thread_id"], found.run_id
                ),
            )
        )

    async def deliver_response(self, command: ResponseDelivery) -> SubmissionReceipt:
        """恢复原 checkpoint／interrupt；新尝试同样显式携带 webhook。"""
        command = ResponseDelivery.model_validate(command.model_dump())
        snapshot, context = self._command(command)
        with self.repository.sessions() as session:
            row = session.get(ContinuationRow, command.request.continuation_ref.continuation_id)
            if row is None or row.reference != command.request.continuation_ref.model_dump(
                mode="json"
            ):
                raise ExecutionConflict("continuation binding is unavailable")
            binding = LangGraphContinuationBinding(
                reference=command.request.continuation_ref, **row.binding
            )
        state = dict(await self.client.threads.get_state(binding.thread_id))
        native_payload = next(
            (item["value"] for item in interrupts(state) if item["id"] == binding.interrupt_id), {}
        )
        response = command.response.model_dump(mode="json")
        if isinstance(command.request, InteractionRequest):
            if "action_requests" in native_payload or "approval_id" in native_payload:
                mapped = {"type": command.response.decision}
                if command.response.reason:
                    mapped["message"] = command.response.reason
                if "approval_id" in native_payload:
                    mapped["arguments_hash"] = native_payload["arguments_hash"]
                response = {"decisions": [mapped]}
            else:
                response.pop("revision")
        native_command = binding.resume_command(state, response)
        run = await self.client.runs.create(
            binding.thread_id,
            snapshot["assistant_id"],
            command=native_command,
            checkpoint=binding.checkpoint,
            context=context.model_dump(mode="json"),
            metadata={
                **context.trace_metadata(),
                "operation_id": command.operation_id,
                "application_run_id": context.run_id,
                "request_id": command.request.request_id,
                "continuation_id": binding.reference.continuation_id,
            },
            webhook=self.settings.coordinator_callback_url,
            multitask_strategy="reject",
        )
        return SubmissionReceipt(
            status="submitted",
            execution_ref=self._reference(
                context.run_id, command.operation_id, binding.thread_id, run["run_id"]
            ),
        )

    async def request_cancel(self, reference, *, operation_id: str) -> CancellationReceipt:
        """精确尝试确认停止，拒绝把本地命令受理当作取消完成。"""
        if reference.backend_instance_id != self.instance:
            raise ExecutionConflict("cancel backend does not match")
        thread_id, run_id = json.loads(reference.execution_id)
        confirmed = await self.native.cancel_run(thread_id=thread_id, run_id=run_id)
        return CancellationReceipt(
            execution_ref=reference, status="confirmed" if confirmed else "requested"
        )

    def _interaction(self, source, state, observation, snapshot, native_run):
        """仅映射发布声明和受治理动作；问题正文不能指定 schema 或权限。"""
        identifier = "interaction-" + digest(
            [source.task_id, source.operation_id, observation.interrupt_id]
        )
        with self.repository.sessions() as session:
            previous = session.get(PendingInteractionRow, identifier)
            if previous and "coordination" in previous.request:
                return InteractionRequest.model_validate(previous.request["coordination"])
            # 只读 shadow 与接管后观察复用旧实例，保留已展示的 ID／revision／期限。
            previous = session.scalar(
                select(PendingInteractionRow).where(
                    PendingInteractionRow.owner_run_id == source.task_id,
                    PendingInteractionRow.server_run_id.in_(
                        [native_run["run_id"], source.operation_id]
                    ),
                    PendingInteractionRow.interrupt_id == observation.interrupt_id,
                )
            )
            if previous and "coordination" in previous.request:
                return InteractionRequest.model_validate(previous.request["coordination"])
            legacy = previous
            if legacy:
                identifier = legacy.interaction_id
            revision = (
                session.scalar(
                    select(func.max(PendingInteractionRow.revision)).where(
                        PendingInteractionRow.owner_run_id == source.task_id
                    )
                )
                or 0
            ) + 1
            if legacy:
                revision = legacy.revision
        definition = self.releases.verify(snapshot)
        payload, action, approval_id = observation.payload, None, None
        allowed = ("approve", "reject")
        if observation.kind == "interaction":
            point = next(
                (p for p in definition.interaction_points if p.point_id == payload.get("point_id")),
                None,
            )
            if point is None or point.kind != payload.get("interaction_kind"):
                raise ExecutionConflict("unpublished interaction point")
            action = payload.get("action")
        elif observation.kind == "hitl":
            action = payload["action_requests"][0]
            config = payload["review_configs"][0]
            if (
                action["name"] not in {ref.tool_id for ref in definition.allowed_tools}
                or config.get("action_name") != action["name"]
            ):
                raise ExecutionConflict("approval action is not pinned in Agent release")
            allowed = tuple(
                value
                for value in config.get("allowed_decisions", ())
                if value in {"approve", "reject"}
            )
            if not allowed:
                raise ExecutionConflict("unsupported approval decisions")
            point = InteractionPoint(
                point_id="tool_approval",
                kind="approval",
                question=f"请确认是否执行 {action['name']}",
                required_scope=self.settings.coordinator_approval_scope,
            )
        else:
            point_def = next(
                (
                    p
                    for p in definition.approval_points
                    if p.approval_id == payload.get("approval_point")
                ),
                None,
            )
            if (
                point_def is None
                or payload.get("workflow_id") != definition.workflow_id
                or payload.get("workflow_version") != definition.version
                or payload.get("arguments_hash") != snapshot["input_hash"]
                or payload.get("required_scope") != point_def.required_scope
                or tuple(payload.get("allowed_decisions", ())) != point_def.allowed_decisions
                or payload.get("requested_action") != point_def.requested_action
            ):
                raise ExecutionConflict("workflow approval does not match its pinned release")
            approval_id, action, allowed = (
                payload["approval_id"],
                payload,
                point_def.allowed_decisions,
            )
            point = InteractionPoint(
                point_id="workflow_approval",
                kind="approval",
                question=f"请确认 {point_def.requested_action}",
                required_scope=point_def.required_scope,
                timeout_seconds=definition.timeout_policy.approval_timeout_seconds,
            )
        expires_at = datetime.fromisoformat(
            state.get("created_at") or native_run["created_at"]
        ) + timedelta(seconds=point.timeout_seconds)
        if legacy:
            from financeclaw.coordination.repository import aware

            if observation.kind == "hitl" and legacy.point_id == action["name"]:
                point = point.model_copy(update={"point_id": legacy.point_id})
            if observation.kind == "workflow" and legacy.point_id == payload["approval_point"]:
                point = point.model_copy(update={"point_id": legacy.point_id})
            if (
                legacy.point_id != point.point_id
                or legacy.kind != point.kind
                or legacy.question != (payload.get("question") or point.question)
                or legacy.checkpoint_id not in {None, state["checkpoint"]["checkpoint_id"]}
                or legacy.response is not None
                or (
                    "native_payload" in legacy.request
                    and legacy.request["native_payload"] != payload
                )
                or ("action" in legacy.request and legacy.request["action"] != action)
            ):
                raise ExecutionConflict("legacy interaction evidence does not match checkpoint")
            expires_at = aware(legacy.expires_at)
        values = {
            "point": point.model_dump(mode="json"),
            "revision": revision,
            "question": payload.get("question") or point.question,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            "action_hash": bounded_digest(action) if action is not None else None,
        }
        if action is not None:
            values["action"] = action
        if approval_id:
            values["approval_id"] = approval_id
        if allowed != ("approve", "reject"):
            values["allowed_decisions"] = list(allowed)
        native = {
            "thread_id": snapshot["thread_id"],
            "run_id": native_run["run_id"],
            "checkpoint": state["checkpoint"],
            "interrupt_id": observation.interrupt_id,
        }
        continuation = ContinuationRef(
            continuation_id="continuation:" + bounded_digest([identifier, native]),
            request_id=identifier,
            source_execution_ref=source,
            release=release_ref(snapshot),
            input_hash=bounded_digest(values),
            binding_hash=bounded_digest(native),
        )
        return InteractionRequest(
            request_id=identifier,
            root_task_id=snapshot["context"]["root_run_id"],
            owner_task_id=source.task_id,
            source_execution_ref=source,
            continuation_ref=continuation,
            input_hash=continuation.input_hash,
            **values,
        )

    async def observe_execution(self, reference) -> BackendObservation:
        """精确尝试与检查点联结后返回业务观察；success 回调不决定业务终态。"""
        if reference.backend_instance_id != self.instance:
            raise ExecutionConflict("observation backend mismatch")
        thread_id, run_id = json.loads(reference.execution_id)
        run = await self.client.runs.get(thread_id, run_id)
        if run.get("metadata", {}).get("operation_id") != reference.operation_id:
            raise ExecutionConflict("run metadata does not match original operation")
        if run["status"] in {"pending", "running"}:
            return BackendObservation(execution_ref=reference, status="active")
        snapshot = self.repository.execution.get(reference.task_id)["snapshot"]
        try:
            state = dict(await self.native._run_state(thread_id, run_id))
        except RuntimeError:
            if run["status"] in {"error", "timeout"}:
                return BackendObservation(
                    execution_ref=reference, status="failed", evidence_ref=reference.execution_id
                )
            raise
        checkpoint_id = state["checkpoint"]["checkpoint_id"]
        proofs = self._application_evidence(reference, run, state)
        if run["status"] in {"error", "timeout"}:
            return BackendObservation(
                execution_ref=reference,
                status="failed",
                evidence_ref=checkpoint_id,
                response_applications=proofs,
            )
        items = interrupts(state)
        observation = observe_run({"status": run["status"], "interrupts": items})
        if items or state.get("next"):
            if len(items) != 1 or observation.kind not in {
                "handoff",
                "interaction",
                "hitl",
                "workflow",
            }:
                return BackendObservation(
                    execution_ref=reference,
                    status="unknown",
                    evidence_ref=checkpoint_id,
                    response_applications=proofs,
                )
            if observation.kind == "handoff":
                kind, target_id, version, _ = self.releases.target(
                    observation.handoff, snapshot, snapshot_context(snapshot).scopes
                )
                if kind.value == "agent":
                    from financeclaw.shared.execution_ledger.snapshots import agent_snapshot

                    target_snapshot = agent_snapshot(
                        self.releases.agents.resolve(target_id, version),
                        snapshot_context(snapshot),
                        thread_id="pending",
                        input_hash="",
                    )
                else:
                    from financeclaw.coordination.workflows.service import WorkflowService

                    target_snapshot = {
                        "release": WorkflowService._release(
                            self.releases.workflows.resolve(target_id, version)
                        )
                    }
                request, binding = map_delegation(
                    source=reference,
                    root_task_id=snapshot["context"]["root_run_id"],
                    parent_release=release_ref(snapshot),
                    target=release_ref(target_snapshot),
                    thread_id=thread_id,
                    run_id=run_id,
                    state=state,
                    interrupt=items[0],
                )
            else:
                request = self._interaction(reference, state, observation, snapshot, run)
                binding = LangGraphContinuationBinding(
                    reference=request.continuation_ref,
                    thread_id=thread_id,
                    run_id=run_id,
                    checkpoint=state["checkpoint"],
                    interrupt_id=items[0]["id"],
                )
            return BackendObservation(
                execution_ref=reference,
                status="waiting",
                requests=(request,),
                continuation_bindings={
                    binding.reference.continuation_id: binding.model_dump(
                        mode="json", exclude={"reference"}
                    )
                },
                evidence_ref=checkpoint_id,
                response_applications=proofs,
            )
        definition = self.releases.verify(snapshot)
        values = state.get("values") or {}
        if "profile" in snapshot:
            output = (
                definition.output_schema.model_validate(
                    values.get(definition.output_state_key)
                ).model_dump(mode="json")
                if snapshot["context"].get("parent_run_id") and definition.output_schema
                else {"message": final_assistant_content(values)}
            )
            if output.get("message", "present") is None:
                raise ExecutionConflict("completed Agent has no final assistant result")
        else:
            # checkpoint values 包含内部 State；只按发布的 output_schema 投影终态。
            candidate = values.get("output", values)
            output = definition.output_schema.model_validate(
                {
                    key: candidate[key]
                    for key in definition.output_schema.model_fields
                    if key in candidate
                }
            ).model_dump(mode="json")
            if (
                output.get("run_id") != reference.task_id
                or output.get("arguments_hash") != snapshot["input_hash"]
            ):
                raise ExecutionConflict("workflow result does not match its frozen execution")
        return BackendObservation(
            execution_ref=reference,
            status="completed",
            result=output,
            evidence_ref=checkpoint_id,
            response_applications=proofs,
        )

    def _application_evidence(self, reference, run, state):
        """父响应必须有原恢复尝试的匹配 ToolMessage；HTTP 接受不足以证明应用。"""
        operation = self.repository.execution.operation(reference.operation_id)
        if operation["request"].get("kind") != "response":
            return ()
        command = ResponseDelivery.model_validate(operation["request"]["payload"])
        metadata = run.get("metadata", {})
        if (
            metadata.get("request_id") != command.request.request_id
            or metadata.get("continuation_id") != command.request.continuation_ref.continuation_id
        ):
            return ()
        if isinstance(command.request, DelegationRequest):
            found = False
            for message in state.get("values", {}).get("messages", ()):
                if message.get("type") != "tool" or not isinstance(message.get("content"), str):
                    continue
                try:
                    found |= json.loads(message["content"]) == command.response.model_dump(
                        mode="json"
                    )
                except (ValueError, TypeError):
                    continue
            if not found:
                return ()
        else:
            # 原生恢复执行已产生属于该 operation 的新检查点，且不再等待原 interrupt。
            with self.repository.sessions() as session:
                row = session.get(ContinuationRow, command.request.continuation_ref.continuation_id)
                if row is None or row.binding["interrupt_id"] in {
                    item["id"] for item in interrupts(state)
                }:
                    return ()
            if state.get("metadata", {}).get("step", -1) < 0:
                return ()
        return (
            ResponseApplicationEvidence(
                operation_id=reference.operation_id,
                request_id=command.request.request_id,
                continuation_id=command.request.continuation_ref.continuation_id,
                execution_ref=reference,
                response_hash=bounded_digest(command.response.model_dump(mode="json")),
                checkpoint_ref=state["checkpoint"]["checkpoint_id"],
            ),
        )
