"""Workflow 应用服务：承载发布型工作流的持久化运行用例与审批治理。

负责受治理启动、状态对账、基于 LangGraph interrupt/resume 的审批（恢复前
复验权限、owner、参数 hash 与过期时间）以及全程审计落档。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from financeclaw.coordination.application.run_observation import observe_run
from financeclaw.coordination.application.run_service import IdempotencyConflict, RunNotFound
from financeclaw.coordination.application.streaming import (
    completed_stream_event,
    failed_stream_event,
    interrupted_stream_event,
    progress_stream_event,
    project_server_part,
)
from financeclaw.coordination.backends.ports.agent_server import AgentServerClient
from financeclaw.coordination.execution.service import ExecutionService
from financeclaw.coordination.interactions.service import (
    InteractionService,
    waiting_reason,
)
from financeclaw.coordination.workflows.repository import (
    WorkflowConflict,
    WorkflowIdempotencyConflict,
    WorkflowNotFound,
    WorkflowRepository,
)
from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.responses import (
    ApprovalDecision,
    ApprovalDecisionType,
    RunAccepted,
    RunStatusResponse,
    StreamEvent,
)
from financeclaw.kernel.targets import WorkflowTarget
from financeclaw.kernel.workflows.catalog import WorkflowCatalog
from financeclaw.kernel.workflows.models import (
    WorkflowApproval,
    WorkflowApprovalStatus,
    WorkflowRun,
    WorkflowRunStatus,
)
from financeclaw.shared.audit.models import AuditEventType, AuditRecord
from financeclaw.shared.audit.repository import AuditRepository
from financeclaw.shared.execution_ledger.repository import ExecutionConflict, snapshot_context

LOGGER = logging.getLogger(__name__)


class WorkflowAuthorizationError(PermissionError):
    """调用方缺少工作流所需权限范围时抛出。"""

    pass


class WorkflowApprovalExpired(RuntimeError):
    """工作流审批窗口已超时，审批单被标记 EXPIRED 后抛出。"""

    pass


class WorkflowInputError(ValueError):
    """工作流入参或审批决定不合法时抛出。"""

    pass


class WorkflowService:
    """发布型工作流的运行用例服务：启动、对账、审批恢复与输出校验。

    使用场景：BFF 或 delegation 服务以 WorkflowTarget 启动工作流；状态轮询时
    依据 server 中断登记审批单；审批方经 resume 恢复执行，恢复前复验权限、
    owner、参数 hash 与过期时间；全程写审计并按 output_schema 校验输出。

    Attributes:
        client: Agent Server 客户端 Port，用于线程与运行管理。
        repository: 工作流仓储，持久化运行、审批单与状态。
        catalog: 已发布工作流目录，提供定义解析与输出 schema。
        audit: 审计仓储，记录工作流全生命周期事件。
        execution: 工作流执行快照与预算仓储，委派运行共享父任务的根预算。
        operations: 持久化 start/resume 协调器，区分提交回执和最终执行结果。
        interactions: 将工作流审批映射为统一交互，并与原审批单原子更新决定。
        _clock: （私有）可注入的 UTC 时钟，便于测试超时逻辑。

    """

    def __init__(
        self,
        client: AgentServerClient,
        repository: WorkflowRepository,
        catalog: WorkflowCatalog,
        audit: AuditRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """装配工作流服务依赖。

        Args:
            client: Agent Server 客户端 Port。
            repository: 工作流仓储实现。
            catalog: 已发布工作流目录。
            audit: 审计仓储。
            clock: 可注入的时钟；缺省取当前 UTC 时间，须返回带时区的时间。

        """
        self.client = client
        self.repository = repository
        self.catalog = catalog
        self.audit = audit
        self._clock = clock or (lambda: datetime.now(UTC))
        self.execution = repository.execution
        self.operations = ExecutionService(client, self.execution)
        self.interactions = InteractionService(
            client, self.execution, workflow_catalog=catalog, clock=lambda: self._clock()
        )

    async def start(
        self,
        target: WorkflowTarget,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
        idempotency_key: str,
        parent_snapshot: dict[str, Any] | None = None,
        parent_run_id: str | None = None,
    ) -> RunAccepted:
        """受治理地启动一次工作流运行（幂等）。

        Args:
            target: 工作流目标（ID、版本与原始入参）。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。
            scopes: 调用方权限范围，用于工作流鉴权。
            idempotency_key: 客户端幂等键，重复提交需保持一致。
            parent_snapshot: 委派来源的冻结授权；独立工作流可不提供。
            parent_run_id: 父运行 ID，使工作流归入根任务树。

        Returns:
            受理结果：业务 run/thread 标识与是否幂等重放。

        Raises:
            WorkflowInputError: 工作流不存在或入参不合法。
            WorkflowAuthorizationError: 缺少所需权限范围。
            IdempotencyConflict: 幂等键已被不同请求占用。

        """
        await asyncio.to_thread(self.execution.require_legacy, parent_run_id)
        try:
            # 1. 解析已发布定义并按 input_schema 归一化入参。
            definition = self.catalog.resolve(target.workflow_id, target.version)
            normalized = definition.normalize_input(target.arguments)
        except Exception as exc:
            raise WorkflowInputError(str(exc)) from exc
        # 2. 校验调用方权限范围。
        self._require_scopes(scopes, definition.required_scopes)
        # 3. 计算参数 hash 与请求指纹（workflow_id+版本+归一化参数）。
        arguments_hash = _hash(normalized)
        request_fingerprint = _hash(
            {
                "workflow_id": definition.workflow_id,
                "workflow_version": definition.version,
                "arguments": normalized,
            }
        )
        try:
            # 4. 以幂等键开启业务运行；重复请求返回既有运行并标记重放。
            record, replay = await asyncio.to_thread(
                self.repository.begin_run,
                definition=definition,
                tenant_id=tenant_id,
                subject_id=subject_id,
                idempotency_key=idempotency_key,
                arguments_hash=arguments_hash,
                request_fingerprint=request_fingerprint,
                input_payload=normalized,
            )
        except WorkflowIdempotencyConflict as exc:
            raise IdempotencyConflict(str(exc)) from exc
        # 5. 首次启动写 WORKFLOW_STARTED 审计。
        if not replay:
            await self._audit(
                record,
                AuditEventType.WORKFLOW_STARTED,
                decision="started",
                payload_hash=arguments_hash,
            )
        if not replay:
            context = (
                snapshot_context(parent_snapshot, scopes).model_copy(
                    update={
                        "run_id": record.run_id,
                        "parent_run_id": parent_run_id,
                        "root_run_id": snapshot_context(parent_snapshot).root_run_id
                        or parent_run_id,
                    }
                )
                if parent_snapshot
                else self._context(record, scopes).model_copy(
                    update={
                        "root_run_id": record.run_id,
                        "request_clock": self._now().isoformat(),
                    }
                )
            )
            await asyncio.to_thread(
                self.execution.register,
                record.run_id,
                {
                    "context": context.model_dump(mode="json"),
                    "thread_id": record.thread_id,
                    "assistant_id": record.assistant_id,
                    "input_hash": record.arguments_hash,
                    "release": self._release(definition),
                    "limits": {"model": 64, "tool": 128, "operation": 64},
                },
                root_run_id=context.root_run_id,
            )
        execution = await asyncio.to_thread(self.execution.get, record.run_id)
        if execution["snapshot"]["release"] != self._release(definition):
            raise ExecutionConflict("pinned workflow release is unavailable")
        context = snapshot_context(execution["snapshot"], scopes)
        if record.server_run_id is None:
            await self.client.create_thread(record.thread_id)
            server_run = await self.operations.submit(
                record.run_id,
                "start",
                thread_id=record.thread_id,
                assistant_id=record.assistant_id,
                input=record.input_payload,
                context=context.model_dump(mode="json"),
                metadata=self._metadata(record, context),
            )
            if server_run is not None:
                record = await asyncio.to_thread(
                    self.repository.bind_server_run,
                    record.run_id,
                    server_run.run_id,
                    server_run.status,
                )
        # 9. 返回受理结果。
        return RunAccepted(
            run_id=record.run_id,
            thread_id=record.thread_id,
            status=record.status.value,
            target_kind="workflow",
            idempotent_replay=replay,
        )

    async def status(
        self, run_id: str, *, tenant_id: str, subject_id: str, scopes: frozenset[str] | None = None
    ) -> RunStatusResponse:
        """查询工作流运行状态，并处理超时、中断登记与结果落库。

        Args:
            run_id: 业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。
            scopes: 当前权限；提供时才允许补发已落决定但尚未领取的恢复操作。

        Returns:
            最新状态响应。

        Raises:
            RunNotFound: 运行不存在或不属于当前主体。

        """
        await asyncio.to_thread(self.execution.require_legacy, run_id)
        record = await self._owned(run_id, tenant_id, subject_id)
        # 1. 终态或尚未绑定 server run：直接返回当前记录。
        if record.status in _TERMINAL:
            return self._response(record)
        await self.interactions.reconcile_owner(run_id, scopes=scopes)
        uncertain = await self.operations.reconcile(run_id)
        execution = await asyncio.to_thread(self.execution.get, run_id)
        if execution["cancellation_requested"]:
            return self._response(record).model_copy(
                update={
                    "waiting_reason": "execution_stop_not_confirmed",
                    "status": "cancellation_requested",
                }
            )
        server_id = execution["server_run_id"]
        if server_id is None or uncertain:
            return self._response(record).model_copy(
                update={"waiting_reason": "submission_uncertain"}
            )

        # 3. 查询 server 状态并按结果分派处理。
        server = await self.client.get_run(
            thread_id=record.thread_id,
            run_id=server_id,
        )
        server_status = str(server.get("status", record.status.value))
        if server_status == "interrupted":
            # 3a. 中断：校验并登记审批单。
            return await self._record_interrupt(record, server, server_run_id=server_id)
        if server_status in {"error", "failed", "timeout"}:
            # 3b. 失败：置 FAILED 并审计 server_failed。
            failed, changed = await asyncio.to_thread(
                self.repository.set_status, record.run_id, WorkflowRunStatus.FAILED
            )
            if changed:
                await self._audit(
                    failed,
                    AuditEventType.WORKFLOW_FAILED,
                    decision="server_failed",
                    payload_hash=failed.arguments_hash,
                )
            return self._response(failed)
        if server_status in {"success", "completed"}:
            # 3c. 成功：取回最终输出并校验落库。
            output = await self.client.join_run(
                thread_id=record.thread_id,
                run_id=server_id,
            )
            if output.get("interrupts") or output.get("__interrupt__"):
                return await self._record_interrupt(record, output, server_run_id=server_id)
            return await self._complete(record, output)
        # 软超时只提示仍需确认停止。每次实际执行尝试独立计时，不消耗审批等待时间。
        operations = await asyncio.to_thread(self.execution.operations_for_run, run_id)
        attempt = next((item for item in operations if item["server_run_id"] == server_id), None)
        started_at = attempt["created_at"] if attempt else record.started_at
        if self._now() >= _aware(started_at) + timedelta(seconds=record.run_timeout_seconds):
            waiting, changed = await asyncio.to_thread(
                self.repository.set_status, run_id, WorkflowRunStatus.INTERRUPTED
            )
            if changed:
                await self._audit(
                    waiting,
                    AuditEventType.WORKFLOW_INTERRUPTED,
                    decision="run_timeout",
                    payload_hash=record.arguments_hash,
                )
            return self._response(waiting).model_copy(
                update={"waiting_reason": "execution_timeout"}
            )
        # 3d. 其余状态同步为 PENDING/RUNNING。
        pending, _ = await asyncio.to_thread(
            self.repository.set_status,
            record.run_id,
            _server_status(server_status),
        )
        return self._response(pending)

    async def resume(
        self,
        run_id: str,
        decision: ApprovalDecision,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
    ) -> RunStatusResponse:
        """提交工作流审批决定并恢复被中断的运行。

        Args:
            run_id: 业务 run ID。
            decision: 审批决定（approve/reject 及理由、参数 hash）。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。
            scopes: 调用方权限范围，用于审批鉴权。

        Returns:
            恢复后的最新状态响应。

        Raises:
            RunNotFound: 运行不存在或不属于当前主体。
            WorkflowConflict: 运行未在等待审批，或参数 hash 与审批单不一致。
            WorkflowInputError: 决定类型不受支持（发布型工作流拒绝 EDIT）。
            WorkflowApprovalExpired: 审批窗口已超时。
            WorkflowAuthorizationError: 缺少审批点所需权限范围。

        """
        await asyncio.to_thread(self.execution.require_legacy, run_id)
        record = await self._owned(run_id, tenant_id, subject_id)
        execution = await asyncio.to_thread(self.execution.get, run_id)
        if execution["cancellation_requested"]:
            raise WorkflowConflict("cancellation requested; approval cannot resume execution")
        if await self.interactions.resume_legacy(
            run_id, decision, tenant_id=tenant_id, subject_id=subject_id, scopes=scopes
        ):
            return await self.status(run_id, tenant_id=tenant_id, subject_id=subject_id)
        waiting = execution["waiting"]
        if not waiting or waiting["kind"] != "workflow":
            raise WorkflowConflict("workflow has no uniquely bound pending approval")
        approval = await asyncio.to_thread(
            self.repository.get_approval, run_id, approval_id=waiting["payload"]["approval_id"]
        )
        self._require_scopes(scopes, frozenset({approval.required_scope}))
        if self._now() >= _aware(approval.expires_at):
            await self._expire_approval(record, approval)
            raise WorkflowApprovalExpired("workflow approval window has expired")
        if decision.type is ApprovalDecisionType.EDIT:
            raise WorkflowInputError("published workflow approval does not allow input edits")
        if (
            decision.type.value not in approval.allowed_decisions
            or decision.arguments_hash != approval.arguments_hash
            or (
                decision.interrupt_id != waiting["key"]
                and (waiting["interrupt_id"] is not None or decision.interrupt_id is not None)
            )
        ):
            raise WorkflowConflict("approval decision does not match the pinned interrupt")
        definition = self.catalog[(record.workflow_id, record.workflow_version)]
        if self._release(definition) != execution["snapshot"]["release"]:
            raise ExecutionConflict("pinned workflow release is unavailable")
        context = snapshot_context(execution["snapshot"], scopes)
        self._require_scopes(context.scopes, definition.required_scopes)
        approval_status = (
            WorkflowApprovalStatus.APPROVED
            if decision.type is ApprovalDecisionType.APPROVE
            else WorkflowApprovalStatus.REJECTED
        )
        decided, changed = await asyncio.to_thread(
            self.repository.decide_approval,
            approval.approval_id,
            status=approval_status,
            decided_by=subject_id,
            reason=decision.reason,
            decided_at=self._now(),
        )
        if changed:
            await self._audit(
                record,
                AuditEventType.WORKFLOW_APPROVED
                if approval_status is WorkflowApprovalStatus.APPROVED
                else AuditEventType.WORKFLOW_REJECTED,
                decision=approval_status.value,
                payload_hash=approval.arguments_hash,
                resource_id=decided.approval_id,
                resource_type="workflow_approval",
            )
        if approval_status is WorkflowApprovalStatus.REJECTED:
            await asyncio.to_thread(self.execution.deny_side_effects, run_id)
        payload = {
            "decisions": [
                {
                    "type": decision.type.value,
                    "arguments_hash": decision.arguments_hash,
                    **({"message": decision.reason} if decision.reason else {}),
                }
            ]
        }
        command = {
            "resume": {waiting["interrupt_id"]: payload} if waiting["interrupt_id"] else payload
        }
        key = "approval:" + waiting["key"]
        server_run = await self.operations.submit(
            run_id,
            key,
            thread_id=record.thread_id,
            assistant_id=record.assistant_id,
            command=command,
            context=context.model_dump(mode="json"),
            metadata=self._metadata(record, context),
            predecessor=waiting["server_run_id"],
        )
        result = await self.operations.result(run_id, key) if server_run is not None else None
        if result is None:
            return self._response(record).model_copy(
                update={
                    "status": "running" if server_run else "interrupted",
                    "waiting_reason": "resume_pending" if server_run else "submission_uncertain",
                }
            )
        if observe_run(result).kind == "completed":
            return await self._complete(record, result)
        if observe_run(result).kind == "failed":
            if result.get("workflow_id") == record.workflow_id:
                return await self._complete(record, result)
            failed, _ = await asyncio.to_thread(
                self.repository.set_status, run_id, WorkflowRunStatus.FAILED
            )
            return self._response(failed)
        return await self._record_interrupt(record, result, server_run_id=server_run.run_id)

    async def cancel(self, run_id: str, *, tenant_id: str, subject_id: str) -> RunStatusResponse:
        """独立 Workflow 使用与根会话相同的停止确认；子 Workflow 通过根任务取消。"""
        await asyncio.to_thread(self.execution.require_legacy, run_id)
        record = await self._owned(run_id, tenant_id, subject_id)
        if record.status in _TERMINAL:
            return self._response(record)
        execution = await asyncio.to_thread(self.execution.get, run_id)
        if execution["root_run_id"] != run_id:
            raise WorkflowConflict("cancel the owning root task, not its delegated Workflow")
        await asyncio.to_thread(self.interactions.repository.cancel_tree, run_id, now=self._now())
        confirmed = await self.operations.confirm_tree_stopped(run_id)
        if confirmed:
            record, changed = await asyncio.to_thread(
                self.repository.set_status, run_id, WorkflowRunStatus.CANCELLED
            )
            if changed:
                await self._audit(
                    record,
                    AuditEventType.WORKFLOW_CANCELLED,
                    decision="execution_stop_confirmed",
                    payload_hash=record.arguments_hash,
                )
        return self._response(record).model_copy(
            update={
                "status": "cancelled" if confirmed else "cancellation_requested",
                "waiting_reason": None if confirmed else "execution_stop_not_confirmed",
            }
        )

    @staticmethod
    def _release(definition: Any) -> dict[str, Any]:
        """发布快照覆盖图身份、Schema、工具绑定和审批策略，而不保存图执行器。"""
        return {
            "workflow_id": definition.workflow_id,
            "version": definition.version,
            "assistant_id": definition.assistant_id,
            "deployment_revision": definition.deployment_revision,
            "required_scopes": sorted(definition.required_scopes),
            "timeout_policy": definition.timeout_policy.model_dump(mode="json"),
            "input_schema": definition.input_schema.model_json_schema(),
            "output_schema": definition.output_schema.model_json_schema(),
            "model_profile_id": definition.model_profile_id,
            "tools": [ref.model_dump(mode="json") for ref in definition.allowed_tools],
            "approval_points": [
                point.model_dump(mode="json") for point in definition.approval_points
            ],
        }

    async def reconcile_incomplete(self) -> tuple[str, ...]:
        """对账所有未完成的工作流运行：刷新状态但不触发审批派发。

        Returns:
            本次完成对账的业务 run ID 列表。

        """
        await asyncio.to_thread(self.execution.require_legacy)
        records = await asyncio.to_thread(self.repository.list_incomplete)
        reconciled: list[str] = []
        for record in records:
            # 1. 逐条触发状态同步（内部会处理超时、中断与完成落库）。
            await self.status(
                record.run_id,
                tenant_id=record.tenant_id,
                subject_id=record.subject_id,
            )
            reconciled.append(record.run_id)
        # 2. 返回已对账的 run ID 列表。
        return tuple(reconciled)

    async def assert_owned(self, run_id: str, *, tenant_id: str, subject_id: str) -> None:
        """校验运行归属于当前租户与主体，不通过则抛出 RunNotFound。

        Args:
            run_id: 业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        """
        await self._owned(run_id, tenant_id, subject_id)

    async def stream(
        self, run_id: str, *, tenant_id: str, subject_id: str
    ) -> AsyncIterator[StreamEvent]:
        """订阅指定工作流 server run，并在流结束后校正业务终态。

        Args:
            run_id: 业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        Yields:
            归一化后的流式事件（事件名 + 数据载荷）。

        Raises:
            RunNotFound: 运行不存在或不属于当前主体。

        """
        await asyncio.to_thread(self.execution.require_legacy, run_id)
        record = await self._owned(run_id, tenant_id, subject_id)
        execution = await asyncio.to_thread(self.execution.get, run_id)
        server_id = execution["server_run_id"]
        if server_id is not None and record.status not in _TERMINAL:
            try:
                async for part in self.client.stream_run(
                    thread_id=record.thread_id,
                    run_id=server_id,
                ):
                    projected = project_server_part(part)
                    if projected is not None:
                        yield projected
            except Exception:
                LOGGER.warning("workflow run stream ended unexpectedly", extra={"run_id": run_id})

        try:
            final = await self.status(run_id, tenant_id=tenant_id, subject_id=subject_id)
        except Exception:
            LOGGER.warning("workflow final reconciliation failed", extra={"run_id": run_id})
            yield failed_stream_event(run_id)
            return
        if final.status == "completed":
            yield completed_stream_event(run_id, final.output or {})
        elif final.status == "interrupted":
            yield interrupted_stream_event(
                run_id,
                waiting_reason=final.waiting_reason,
                pending_interactions=final.pending_interactions,
            )
        elif final.status in {"failed", "rejected"}:
            yield failed_stream_event(run_id)
        else:
            yield progress_stream_event(run_id, final.status)

    async def _record_interrupt(
        self, record: WorkflowRun, server: Mapping[str, Any], *, server_run_id: str
    ) -> RunStatusResponse:
        """校验 server 中断载荷并登记工作流审批单。

        Args:
            record: 工作流运行记录。
            server: server 运行详情（含中断载荷）。
            server_run_id: 此次查询的确切尝试；不能把旧响应挂到并发更新后的最新 run。

        Returns:
            状态为 INTERRUPTED 的响应，附带审批载荷与过期时间。

        Raises:
            WorkflowConflict: 中断载荷与业务运行或已发布定义不一致。

        """
        execution = await asyncio.to_thread(self.execution.get, record.run_id)
        if execution["server_run_id"] != server_run_id:
            raise WorkflowConflict("stale workflow observation; query the current attempt")
        # 1. 提取审批载荷，并复验 workflow_id/版本/参数 hash 与业务运行一致。
        observation = observe_run(server)
        if observation.kind != "workflow":
            waiting, _ = await asyncio.to_thread(
                self.repository.set_status, record.run_id, WorkflowRunStatus.INTERRUPTED
            )
            return self._response(waiting).model_copy(
                update={"waiting_reason": "unsupported_interruption"}
            )
        payload = _interrupt_payload(server)
        if (
            payload.get("workflow_id") != record.workflow_id
            or payload.get("workflow_version") != record.workflow_version
            or payload.get("arguments_hash") != record.arguments_hash
        ):
            raise WorkflowConflict("Agent Server returned a mismatched workflow approval")
        try:
            # 2. 审批点必须属于已发布定义。
            definition = self.catalog[(record.workflow_id, record.workflow_version)]
            approval_point = next(
                point
                for point in definition.approval_points
                if point.approval_id == payload["approval_point"]
            )
        except (KeyError, StopIteration) as exc:
            raise WorkflowConflict("workflow returned an unpublished approval point") from exc
        # 3. 决策列表、所需权限与请求动作必须与发布版本一致。
        if (
            tuple(payload["allowed_decisions"]) != approval_point.allowed_decisions
            or payload["required_scope"] != approval_point.required_scope
            or payload["requested_action"] != approval_point.requested_action
        ):
            raise WorkflowConflict("workflow approval policy differs from its published release")
        # 4. 构建审批单（含过期时间）并幂等落库。
        requested_at = self._now()
        approval = WorkflowApproval(
            approval_id=str(payload["approval_id"]),
            run_id=record.run_id,
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            approval_point=str(payload["approval_point"]),
            arguments_hash=record.arguments_hash,
            requested_action=approval_point.requested_action,
            request_payload=dict(payload),
            allowed_decisions=approval_point.allowed_decisions,
            required_scope=approval_point.required_scope,
            status=WorkflowApprovalStatus.PENDING,
            requested_at=requested_at,
            expires_at=requested_at + timedelta(seconds=record.approval_timeout_seconds),
        )
        saved, created = await asyncio.to_thread(self.repository.ensure_approval, approval)
        await asyncio.to_thread(
            self.execution.set_waiting,
            record.run_id,
            {
                "key": observation.interrupt_id or saved.approval_id,
                "interrupt_id": observation.interrupt_id,
                "kind": "workflow",
                "server_run_id": server_run_id,
                "payload": payload,
                "payload_hash": _hash(payload),
                "expires_at": _aware(saved.expires_at).isoformat(),
            },
            server_run_id=server_run_id,
        )
        interrupted, changed = await asyncio.to_thread(
            self.repository.set_status, record.run_id, WorkflowRunStatus.INTERRUPTED
        )
        # 5. 首次登记或状态变化时写 INTERRUPTED 审计。
        if created or changed:
            await self._audit(
                interrupted,
                AuditEventType.WORKFLOW_INTERRUPTED,
                decision="approval_requested",
                payload_hash=record.arguments_hash,
                resource_id=saved.approval_id,
                resource_type="workflow_approval",
            )
        # 6. 返回携带审批载荷与过期时间的响应。
        expired = self._now() >= _aware(saved.expires_at)
        if observation.interrupt_id:
            interaction = await self.interactions.observe_workflow(
                record.run_id,
                observation,
                saved,
                server_run_id=server_run_id,
                checkpoint_id=(server.get("checkpoint") or {}).get("checkpoint_id"),
            )
            public = await self.interactions.public(interaction)
            return RunStatusResponse(
                run_id=record.run_id,
                thread_id=record.thread_id,
                status="interrupted",
                waiting_reason=waiting_reason(public),
                pending_interactions=(public,),
            )
        if expired and saved.status is WorkflowApprovalStatus.PENDING:
            await self._expire_approval(record, saved)
        return RunStatusResponse(
            run_id=record.run_id,
            thread_id=record.thread_id,
            status=WorkflowRunStatus.INTERRUPTED.value,
            waiting_reason="approval_expired" if expired else "approval_required",
            pending_interactions=(
                {
                    "interrupt_id": observation.interrupt_id or saved.approval_id,
                    "kind": "approval",
                    "approval_id": saved.approval_id,
                    "arguments_hash": saved.arguments_hash,
                    "allowed_decisions": list(saved.allowed_decisions),
                    "expires_at": _aware(saved.expires_at).isoformat(),
                },
            ),
            output={
                "approval": saved.request_payload,
                "expires_at": _aware(saved.expires_at).isoformat(),
            },
        )

    async def _expire_approval(self, record: WorkflowRun, approval: WorkflowApproval) -> None:
        """审批超时只结束该决定窗口，不虚构远程执行失败或取消。"""
        if approval.status is not WorkflowApprovalStatus.PENDING:
            return
        _, changed = await asyncio.to_thread(
            self.repository.decide_approval,
            approval.approval_id,
            status=WorkflowApprovalStatus.EXPIRED,
            decided_by=record.subject_id,
            reason="approval timeout",
            decided_at=self._now(),
        )
        if changed:
            await self._audit(
                record,
                AuditEventType.WORKFLOW_INTERRUPTED,
                decision="approval_timeout",
                payload_hash=approval.arguments_hash,
                resource_id=approval.approval_id,
                resource_type="workflow_approval",
            )

    async def _complete(
        self, record: WorkflowRun, raw_output: Mapping[str, Any]
    ) -> RunStatusResponse:
        """校验工作流最终输出并按结果落库（含审计与 artifact 引用）。

        Args:
            record: 工作流运行记录。
            raw_output: server 运行结束后的原始输出。

        Returns:
            落库后的最新状态响应（COMPLETED/REJECTED/FAILED）。

        Raises:
            WorkflowConflict: 输出不符合已发布 schema 或与业务运行不匹配。

        """
        output = dict(raw_output)
        # 1. 输出包裹在 response 字段内时先行解包。
        if isinstance(output.get("response"), Mapping):
            output = dict(output["response"])
        try:
            # 2. 按 output_schema 校验，并复核与业务运行的绑定关系。
            definition = self.catalog[(record.workflow_id, record.workflow_version)]
            # SDK 按确切 Run 返回完整 checkpoint state；仅提取已发布的输出投影。
            output = {
                key: value
                for key, value in output.items()
                if key in definition.output_schema.model_fields
            }
            validated = definition.output_schema.model_validate(output).model_dump(mode="json")
            if (
                validated.get("workflow_id") != record.workflow_id
                or validated.get("workflow_version") != record.workflow_version
                or validated.get("run_id") != record.run_id
                or validated.get("arguments_hash") != record.arguments_hash
            ):
                raise ValueError("workflow output does not match its pinned business run")
        except Exception as exc:
            # 3. 校验失败：置 FAILED、审计 invalid_output 并抛出。
            failed, changed = await asyncio.to_thread(
                self.repository.set_status, record.run_id, WorkflowRunStatus.FAILED
            )
            if changed:
                await self._audit(
                    failed,
                    AuditEventType.WORKFLOW_FAILED,
                    decision="invalid_output",
                    payload_hash=record.arguments_hash,
                )
            raise WorkflowConflict("workflow returned invalid published output") from exc
        # 4. 按输出声明状态落库，并登记 artifact 引用。
        result_status = WorkflowRunStatus(str(validated["status"]))
        artifact = validated.get("artifact")
        artifact_refs = (str(artifact["artifact_id"]),) if isinstance(artifact, Mapping) else ()
        completed, changed = await asyncio.to_thread(
            self.repository.set_status,
            record.run_id,
            result_status,
            output_payload=validated,
            artifact_refs=artifact_refs,
        )
        # 5. 状态实际变化时分别写 COMPLETED/FAILED 审计。
        if changed and result_status is WorkflowRunStatus.COMPLETED:
            await self._audit(
                completed,
                AuditEventType.WORKFLOW_COMPLETED,
                decision="completed",
                payload_hash=_hash(validated),
                artifact_refs=artifact_refs,
            )
        if changed and result_status is WorkflowRunStatus.FAILED:
            await self._audit(
                completed,
                AuditEventType.WORKFLOW_FAILED,
                decision="workflow_failed",
                payload_hash=_hash(validated),
            )
        return self._response(completed)

    async def _owned(self, run_id: str, tenant_id: str, subject_id: str) -> WorkflowRun:
        """加载归属于当前租户与主体的工作流运行。

        Args:
            run_id: 业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        Returns:
            工作流运行记录。

        Raises:
            RunNotFound: 运行不存在或不属于当前主体。

        """
        try:
            return await asyncio.to_thread(self.repository.get_owned, run_id, tenant_id, subject_id)
        except WorkflowNotFound as exc:
            raise RunNotFound(str(exc)) from exc

    async def _audit(
        self,
        record: WorkflowRun,
        event: AuditEventType,
        *,
        decision: str,
        payload_hash: str,
        resource_id: str | None = None,
        resource_type: str = "workflow",
        artifact_refs: tuple[str, ...] = (),
    ) -> None:
        """写一条工作流审计记录（策略版本固定为 workflow-policy/1.0.0）。

        Args:
            record: 工作流运行记录。
            event: 审计事件类型。
            decision: 审计决策描述（如 started、run_timeout）。
            payload_hash: 相关载荷的 SHA-256 摘要。
            resource_id: 审计资源 ID；缺省取工作流 ID。
            resource_type: 审计资源类型（workflow 或 workflow_approval）。
            artifact_refs: 关联的 artifact ID 列表。

        """
        audit = AuditRecord(
            event_type=event,
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            conversation_id=None,
            turn_id=self._turn_id(record),
            run_id=record.run_id,
            resource_type=resource_type,
            resource_id=resource_id or record.workflow_id,
            resource_version=record.workflow_version,
            action="execute" if resource_type == "workflow" else "approve",
            decision=decision,
            policy_version="workflow-policy/1.0.0",
            payload_hash=payload_hash,
            artifact_refs=artifact_refs,
            metadata={
                "assistant_id": record.assistant_id,
                "deployment_revision": record.deployment_revision,
                "model_profile_id": record.model_profile_id,
            },
        )
        await asyncio.to_thread(self.audit.append, audit)

    @staticmethod
    def _context(record: WorkflowRun, scopes: frozenset[str]) -> ExecutionContext:
        """为工作流运行构建随 server 调用下发的执行上下文。

        Args:
            record: 工作流运行记录。
            scopes: 调用方权限范围。

        Returns:
            ExecutionContext 实例（turn_id 由运行 ID 派生）。

        """
        return ExecutionContext(
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            scopes=scopes,
            turn_id=WorkflowService._turn_id(record),
            run_id=record.run_id,
        )

    @staticmethod
    def _turn_id(record: WorkflowRun) -> str:
        """由业务 run ID 派生工作流的稳定 turn 标识。

        Args:
            record: 工作流运行记录。

        Returns:
            形如 "workflow-<run_id>" 的标识字符串。

        """
        return f"workflow-{record.run_id.removeprefix('run-')}"

    @staticmethod
    def _metadata(record: WorkflowRun, context: ExecutionContext) -> dict[str, Any]:
        """构建写入 server run 的追踪元数据。

        Args:
            record: 工作流运行记录。
            context: 执行上下文，提供基础追踪字段。

        Returns:
            以 application_run_id 承载业务 run 映射的元数据字典。

        """
        metadata = {
            **context.trace_metadata(),
            "stage": "4",
            "target_kind": "workflow",
            "workflow_id": record.workflow_id,
            "workflow_version": record.workflow_version,
            "deployment_revision": record.deployment_revision,
            "model_profile_id": record.model_profile_id,
            "arguments_hash": record.arguments_hash,
        }
        metadata["application_run_id"] = metadata.pop("run_id")
        return metadata

    @staticmethod
    def _require_scopes(granted: frozenset[str], required: frozenset[str]) -> None:
        """校验授予权限范围覆盖所需范围（通配 "*" 直接放行）。

        Args:
            granted: 调用方被授予的权限范围。
            required: 工作流或审批点所需的权限范围。

        Raises:
            WorkflowAuthorizationError: 所需范围未被覆盖。

        """
        if "*" not in granted and not required.issubset(granted):
            raise WorkflowAuthorizationError("required workflow scope is missing")

    def _now(self) -> datetime:
        """取当前时间；时钟未返回带时区时间时视为配置错误。

        Returns:
            带时区的当前时间。

        Raises:
            TypeError: 注入时钟返回了 naive datetime。

        """
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("workflow clock must return a timezone-aware datetime")
        return value

    @staticmethod
    def _response(record: WorkflowRun) -> RunStatusResponse:
        """把运行记录投影为状态响应。

        Args:
            record: 工作流运行记录。

        Returns:
            含 run/thread 标识、状态与输出载荷的响应。

        """
        return RunStatusResponse(
            run_id=record.run_id,
            thread_id=record.thread_id,
            status=record.status.value,
            output=record.output_payload,
        )


# 工作流的终态集合：进入后不再轮询 server，也不再接受审批恢复。
_TERMINAL = {
    WorkflowRunStatus.COMPLETED,
    WorkflowRunStatus.REJECTED,
    WorkflowRunStatus.FAILED,
    WorkflowRunStatus.CANCELLED,
}


def _server_status(value: str) -> WorkflowRunStatus:
    """把 server 中间态字符串映射为工作流状态（running 之外视为 PENDING）。

    Args:
        value: server 返回的运行状态字符串。

    Returns:
        对应的工作流运行状态。

    """
    if value == "running":
        return WorkflowRunStatus.RUNNING
    return WorkflowRunStatus.PENDING


def _aware(value: datetime) -> datetime:
    """把 naive datetime 补充为 UTC 时区，aware 值原样返回。

    Args:
        value: 待规范化的时间。

    Returns:
        保证带时区的时间值。

    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _hash(value: Mapping[str, Any]) -> str:
    """对映射做规范化 JSON 序列化并计算 SHA-256 摘要。

    Args:
        value: 待摘要的映射（如归一化入参或完整输出）。

    Returns:
        64 位十六进制摘要字符串。

    """
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _interrupt_payload(server: Mapping[str, Any]) -> dict[str, Any]:
    """从 server 中断详情中提取并校验审批载荷的完整性。

    Args:
        server: server 运行详情映射。

    Returns:
        含审批 ID、审批点、所需权限等必填字段的载荷字典。

    Raises:
        WorkflowConflict: 缺少中断载荷、载荷非对象或必填字段缺失。

    """
    interrupts = server.get("interrupts", server.get("__interrupt__"))
    if not isinstance(interrupts, list | tuple) or len(interrupts) != 1:
        raise WorkflowConflict("workflow interruption is missing approval payload")
    first = interrupts[0]
    if hasattr(first, "value"):
        value = first.value
    elif isinstance(first, Mapping):
        value = first.get("value", first)
    else:
        value = None
    if not isinstance(value, Mapping):
        raise WorkflowConflict("workflow approval payload must be an object")
    required = {
        "approval_id",
        "approval_point",
        "workflow_id",
        "workflow_version",
        "requested_action",
        "arguments_hash",
        "allowed_decisions",
        "required_scope",
    }
    if not required.issubset(value):
        raise WorkflowConflict("workflow approval payload is incomplete")
    return dict(value)
