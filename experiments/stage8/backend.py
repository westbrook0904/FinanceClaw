"""真实 LangGraph 探针 Adapter，冻结绑定保存在实验临时目录。"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from langgraph_sdk.errors import NotFoundError

from financeclaw.coordination.backends.langgraph import LangGraphAgentServerClient
from financeclaw.coordination.backends.langgraph_protocol import (
    LangGraphContinuationBinding,
    execution_id,
    interrupts,
    map_delegation,
)
from financeclaw.kernel.coordination import (
    BackendExecutionRef,
    BackendObservation,
    ContinuationRef,
    InteractionRequest,
    ReleaseRef,
    ResponseApplicationEvidence,
    ResponseDelivery,
    TaskSubmission,
    bounded_digest,
)
from financeclaw.kernel.interactions import InteractionPoint


def release(target: str) -> ReleaseRef:
    """实验图固定发布；真实环境必须使用产品 release catalog 的 fingerprint。"""
    return ReleaseRef(
        kind="agent",
        target_id="probe_" + target,
        version="1.0.0",
        fingerprint=bounded_digest(["probe", target, "1.0.0"]),
    )


class ProbeBackend:
    """只用真实 HTTP Run API 推进合成图；并不宣称是可部署的第二种产品 backend。"""

    def __init__(self, url: str, callback: str, directory: Path) -> None:
        self.native = LangGraphAgentServerClient(url=url)
        self.client = self.native._client
        self.callback = callback
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def _path(self, identity: str) -> Path:
        """文件名由 hash 产生，不把 backend 数据解释成任意路径。"""
        return self.directory / (bounded_digest(identity) + ".json")

    async def submit(self, command: TaskSubmission) -> BackendExecutionRef:
        """预分配 thread，幂等 ensure 后提交一次；调用方持有业务唯一提交权。"""
        command = TaskSubmission.model_validate(command.model_dump())
        thread_id = str(uuid5(NAMESPACE_URL, "stage8:" + command.task_id))
        await self.native.create_thread(thread_id)
        run = await self.client.runs.create(
            thread_id,
            command.release.target_id.removeprefix("probe_"),
            input=command.input,
            metadata={
                "operation_id": command.operation_id,
                "task_id": command.task_id,
                "root_task_id": command.root_task_id,
                "release": command.release.model_dump(mode="json"),
            },
            webhook=self.callback,
            multitask_strategy="reject",
        )
        return BackendExecutionRef(
            backend_instance_id=command.backend_instance_id,
            task_id=command.task_id,
            operation_id=command.operation_id,
            execution_id=execution_id(thread_id, str(run["run_id"])),
        )

    async def lookup(
        self, command: TaskSubmission | ResponseDelivery
    ) -> BackendExecutionRef | None:
        """按原 operation 查回执；None 留给协调器保留 unknown，绝不在此重新提交。"""
        if isinstance(command, TaskSubmission):
            task_id, instance = command.task_id, command.backend_instance_id
            thread_id = str(uuid5(NAMESPACE_URL, "stage8:" + task_id))
        else:
            source = command.request.source_execution_ref
            task_id, instance = source.task_id, source.backend_instance_id
            thread_id, _ = json.loads(source.execution_id)
        try:
            found = await self.native.find_operation(
                thread_id=thread_id, operation_id=command.operation_id
            )
        except NotFoundError:
            return None
        return (
            None
            if found is None
            else BackendExecutionRef(
                backend_instance_id=instance,
                task_id=task_id,
                operation_id=command.operation_id,
                execution_id=execution_id(thread_id, found.run_id),
            )
        )

    async def observe(self, reference: BackendExecutionRef) -> BackendObservation:
        """精确 run 与检查点联结后才投影请求，旧 run 从 history 查询。"""
        thread_id, run_id = json.loads(reference.execution_id)
        run = await self.client.runs.get(thread_id, run_id)
        if run["metadata"].get("operation_id") != reference.operation_id:
            raise ValueError("probe reference does not match operation metadata")
        if run["status"] in {"pending", "running"}:
            return BackendObservation(execution_ref=reference, status="active")
        if run["status"] in {"error", "timeout"}:
            return BackendObservation(execution_ref=reference, status="failed")
        state = dict(await self.native._run_state(thread_id, run_id))
        items = interrupts(state)
        requests = []
        for item in items:
            native = {
                "thread_id": thread_id,
                "run_id": run_id,
                "checkpoint": state["checkpoint"],
                "interrupt_id": item["id"],
            }
            if item["value"].get("kind") == "interaction":
                point = InteractionPoint(
                    point_id="probe_scope",
                    kind="input",
                    question="Confirm synthetic scope",
                    response_schema={
                        "type": "object",
                        "properties": {"scope": {"const": "synthetic"}},
                        "required": ["scope"],
                        "additionalProperties": False,
                    },
                )
                values = {
                    "point": point.model_dump(mode="json"),
                    "revision": 1,
                    "question": point.question,
                    "action_hash": None,
                    "expires_at": (
                        datetime.fromisoformat(run["created_at"]) + timedelta(minutes=15)
                    ).isoformat(),
                }
                # Normalize the timestamp exactly as Pydantic does when hashing the contract.
                values["expires_at"] = values["expires_at"].replace("+00:00", "Z")
                request_id = item["value"]["request_id"]
                continuation = ContinuationRef(
                    continuation_id="continuation:" + bounded_digest(native),
                    request_id=request_id,
                    source_execution_ref=reference,
                    release=ReleaseRef.model_validate(run["metadata"]["release"]),
                    input_hash=bounded_digest(values),
                    binding_hash=bounded_digest(native),
                )
                binding = LangGraphContinuationBinding(reference=continuation, **native)
                request = InteractionRequest(
                    request_id=request_id,
                    root_task_id=run["metadata"]["root_task_id"],
                    owner_task_id=reference.task_id,
                    source_execution_ref=reference,
                    continuation_ref=continuation,
                    input_hash=continuation.input_hash,
                    **values,
                )
            else:
                request, binding = map_delegation(
                    source=reference,
                    root_task_id=run["metadata"]["root_task_id"],
                    parent_release=ReleaseRef.model_validate(run["metadata"]["release"]),
                    target=release("child"),
                    thread_id=thread_id,
                    run_id=run_id,
                    state=state,
                    interrupt=item,
                )
            destination = self._path(binding.reference.continuation_id)
            from uuid import uuid4

            temporary = destination.with_suffix("." + uuid4().hex + ".tmp")
            temporary.write_text(binding.model_dump_json())
            temporary.replace(destination)
            requests.append(request)
        return BackendObservation(
            execution_ref=reference,
            status="waiting" if requests else "completed",
            requests=tuple(requests),
            result=state.get("values", {}).get("output"),
            evidence_ref=state["checkpoint"]["checkpoint_id"],
        )

    async def cancel(self, reference: BackendExecutionRef) -> bool:
        """核心不解释原生 thread／run；停止证据限于确切尝试。"""
        thread_id, run_id = json.loads(reference.execution_id)
        return await self.native.cancel_run(thread_id=thread_id, run_id=run_id)

    async def deliver(self, command: ResponseDelivery) -> BackendExecutionRef:
        """从持久化 binding 恢复，显式带回调并固定 checkpoint／interrupt／前驱。"""
        command = ResponseDelivery.model_validate(command.model_dump())
        binding = LangGraphContinuationBinding.model_validate_json(
            self._path(command.request.continuation_ref.continuation_id).read_text()
        )
        if binding.reference != command.request.continuation_ref:
            raise ValueError("probe continuation was replaced")
        state = await self.client.threads.get_state(binding.thread_id)
        response = (
            command.response.answer
            if isinstance(command.request, InteractionRequest)
            else command.response.model_dump(mode="json")
        )
        resume = binding.resume_command(dict(state), response)
        run = await self.client.runs.create(
            binding.thread_id,
            binding.reference.release.target_id.removeprefix("probe_"),
            command=resume,
            checkpoint=binding.checkpoint,
            metadata={
                "operation_id": command.operation_id,
                "request_id": command.request.request_id,
                "continuation_id": binding.reference.continuation_id,
                "release": binding.reference.release.model_dump(mode="json"),
                "root_task_id": command.request.root_task_id,
            },
            webhook=self.callback,
            multitask_strategy="reject",
        )
        source = command.request.source_execution_ref
        return BackendExecutionRef(
            backend_instance_id=source.backend_instance_id,
            task_id=source.task_id,
            operation_id=command.operation_id,
            execution_id=execution_id(binding.thread_id, str(run["run_id"])),
        )

    async def application_evidence(
        self, command: ResponseDelivery, reference: BackendExecutionRef
    ) -> ResponseApplicationEvidence | None:
        """核对原恢复操作、请求及对应 ToolMessage，子成功不能代替父应用证据。"""
        thread_id, run_id = json.loads(reference.execution_id)
        run = await self.client.runs.get(thread_id, run_id)
        metadata = run["metadata"]
        if (
            metadata.get("operation_id") != command.operation_id
            or metadata.get("request_id") != command.request.request_id
            or metadata.get("continuation_id") != command.request.continuation_ref.continuation_id
        ):
            return None
        state = await self.native._run_state(thread_id, run_id)
        if not any(
            message.get("type") == "tool"
            and message.get("tool_call_id") == command.request.request_id
            and json.loads(message["content"]) == command.response.model_dump(mode="json")
            for message in state.get("values", {}).get("messages", [])
        ):
            return None
        return ResponseApplicationEvidence(
            operation_id=command.operation_id,
            request_id=command.request.request_id,
            continuation_id=command.request.continuation_ref.continuation_id,
            execution_ref=reference,
            response_hash=bounded_digest(command.response.model_dump(mode="json")),
            checkpoint_ref=state["checkpoint"]["checkpoint_id"],
        )
