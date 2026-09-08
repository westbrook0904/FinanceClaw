"""delegation 应用服务：接收顶层 Agent 的 typed handoff，治理化地启动子运行。

把 Workflow 或领域 Agent 作为受治理的 delegation Tool 派发为独立 child
thread/run，维护永久父子映射，并把子结果交付回父运行，全程落审计。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from financeclaw.coordination.application.run_observation import observe_run
from financeclaw.coordination.application.run_service import RunNotFound
from financeclaw.coordination.backends.ports.agent_server import AgentServerClient
from financeclaw.coordination.delegation.context import resolve_context_refs
from financeclaw.coordination.delegation.repository import (
    DelegationConflict,
    DelegationNotFound,
    DelegationRepository,
)
from financeclaw.coordination.execution.service import ExecutionService
from financeclaw.coordination.interactions.service import (
    InteractionService,
    waiting_reason,
)
from financeclaw.coordination.workflows.service import (
    WorkflowAuthorizationError,
    WorkflowInputError,
    WorkflowService,
)
from financeclaw.kernel.agents import AgentProfileCatalog
from financeclaw.kernel.delegation.models import (
    AgentDelegationInput,
    DelegationKind,
    DelegationRecord,
    DelegationResult,
    DelegationStatus,
    HandoffRequest,
    WorkflowHandoff,
)
from financeclaw.kernel.responses import ApprovalDecision, RunStatusResponse
from financeclaw.kernel.targets import WorkflowTarget
from financeclaw.kernel.workflows.models import WorkflowRunStatus
from financeclaw.shared.audit.models import AuditEventType, AuditRecord
from financeclaw.shared.audit.repository import AuditRepository
from financeclaw.shared.execution_ledger.repository import ExecutionConflict, snapshot_context
from financeclaw.shared.execution_ledger.snapshots import agent_snapshot, verify_agent_snapshot


class DelegationInputError(ValueError):
    """handoff 请求非法（目标不可用、参数不合法或父引用不匹配）时抛出。"""

    pass


class DelegationAuthorizationError(PermissionError):
    """调用方缺少 delegation 目标所需权限范围时抛出。"""

    pass


class DelegationService:
    """delegation 用例服务：把 typed handoff 落地为受治理的子运行。

    使用场景：顶层 finance_agent 通过 ReAct 决定把任务交给 Workflow 或领域
    Agent 时，以 delegation Tool 的形式发起 typed handoff；本服务校验父引用
    与权限、幂等落库、启动独立 child thread/run，并把子结果交付回父运行。

    Attributes:
        client: Agent Server 客户端 Port，用于子线程与子运行管理。
        repository: delegation 仓储，持久化父子映射与状态流转。
        workflow_service: 工作流服务，负责 Workflow 类子运行的启动与审批。
        agent_profiles: Agent Profile 目录，用于解析领域 Agent 目标。
        audit: 审计仓储，记录 delegation 全生命周期的审计事件。
        execution: 父子运行共用的执行事实与根任务预算仓储。
        operations: 子运行启动及父结果交付的出站操作协调器。
        interactions: 子运行交互服务；问题归原 owner，父 Agent 不代答。
        conversation_repository: 解析已授权消息引用，不自动注入根会话完整历史。
        artifact_service: 按可信身份读取委派显式引用的制品。

    """

    def __init__(
        self,
        client: AgentServerClient,
        repository: DelegationRepository,
        workflow_service: WorkflowService,
        agent_profiles: AgentProfileCatalog,
        audit: AuditRepository,
        *,
        conversation_repository: Any = None,
        artifact_service: Any = None,
    ) -> None:
        """装配 delegation 服务依赖。

        Args:
            client: Agent Server 客户端 Port。
            repository: delegation 仓储实现。
            workflow_service: 工作流服务。
            agent_profiles: Agent Profile 目录。
            audit: 审计仓储。
            conversation_repository: 授权消息引用解析仓储，不自动注入历史。
            artifact_service: 授权 Artifact 引用解析服务。

        """
        self.client = client
        self.repository = repository
        self.workflow_service = workflow_service
        self.agent_profiles = agent_profiles
        self.audit = audit
        self.execution = repository.execution
        self.operations = ExecutionService(client, self.execution)
        self.interactions = InteractionService(
            client,
            self.execution,
            agent_profiles=agent_profiles,
            workflow_catalog=workflow_service.catalog,
        )
        self.conversation_repository = conversation_repository
        self.artifact_service = artifact_service

    async def start(
        self,
        handoff: HandoffRequest,
        *,
        parent_run_id: str,
        parent_turn_id: str,
        conversation_id: str,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
        parent_snapshot: dict[str, Any] | None = None,
        parent_server_run_id: str | None = None,
        parent_interrupt_id: str | None = None,
    ) -> DelegationRecord:
        """受理 typed handoff：校验、幂等落库并启动（或复用）子运行。

        Args:
            handoff: 顶层 Agent 发出的 typed handoff 请求。
            parent_run_id: 父业务 run ID。
            parent_turn_id: 父业务 Turn ID。
            conversation_id: 所属会话 ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。
            scopes: 调用方权限范围，用于目标解析鉴权。
            parent_snapshot: 父运行冻结的身份、授权和发布快照。
            parent_server_run_id: 产生此次 handoff 的确切执行尝试。
            parent_interrupt_id: 原生 handoff ID；旧单中断可缺省。

        Returns:
            delegation 记录（含子运行映射与最新状态）。

        Raises:
            DelegationInputError: 父引用不匹配、目标不可用或参数非法。
            DelegationAuthorizationError: 缺少目标所需权限范围。

        """
        await asyncio.to_thread(self.execution.require_legacy, parent_run_id)
        # 1. 校验 handoff 中的父 run/turn/会话引用与当前 Turn 一致。
        self._verify_parent(
            handoff,
            parent_run_id=parent_run_id,
            parent_turn_id=parent_turn_id,
            conversation_id=conversation_id,
        )
        # 2. 解析目标（Workflow 定义或 Agent Profile）并校验所需权限范围。
        if parent_snapshot is None:
            parent = await asyncio.to_thread(self.execution.get, parent_run_id)
            parent_snapshot = parent["snapshot"]
            parent_server_run_id = parent["server_run_id"]
        context = snapshot_context(parent_snapshot, scopes)
        parent = await asyncio.to_thread(self.execution.get, parent_run_id)
        if parent["cancellation_requested"] or parent["side_effects_denied"]:
            raise ExecutionConflict("parent no longer permits new delegations")
        kind, target_id, target_version, arguments = self._resolve(
            handoff, context.scopes, parent_snapshot
        )
        try:
            existing = await asyncio.to_thread(
                self.repository.get_owned, handoff.handoff_id, tenant_id, subject_id
            )
        except DelegationNotFound:
            existing = None
        references = (
            existing.execution_snapshot.get("resolved_context", [])
            if (existing is not None and existing.execution_snapshot)
            else (
                await asyncio.to_thread(
                    self._resolve_context_refs,
                    tuple(arguments.get("context_refs", ())),
                    context=context,
                )
                if kind is DelegationKind.AGENT
                else []
            )
        )
        execution_snapshot = {
            "context": context.model_dump(mode="json"),
            "parent_server_run_id": parent_server_run_id,
            "parent_interrupt_id": parent_interrupt_id,
            "resolved_context": references,
            "target_release": agent_snapshot(
                self.agent_profiles.resolve(target_id, target_version),
                context,
                thread_id="delegation-not-started",
                input_hash="",
            )
            if kind is DelegationKind.AGENT
            else self.workflow_service._release(
                self.workflow_service.catalog[(target_id, target_version)]
            ),
        }
        # 3. 以 handoff_id 为幂等键落库；重复请求复用既有记录。
        record, created = await asyncio.to_thread(
            self.repository.ensure_requested,
            delegation_id=handoff.handoff_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            conversation_id=conversation_id,
            parent_turn_id=parent_turn_id,
            parent_run_id=parent_run_id,
            kind=kind,
            target_id=target_id,
            target_version=target_version,
            arguments=arguments,
            execution_snapshot=execution_snapshot,
        )
        # 4. 首次创建时写入 REQUESTED 审计。
        if created:
            await self._audit(
                record,
                AuditEventType.DELEGATION_REQUESTED,
                decision="requested",
            )
        # 5. 尚未启动子运行：按目标类型启动 Workflow 或领域 Agent。
        if record.child_run_id is None:
            if record.kind is DelegationKind.WORKFLOW:
                record = await self._start_workflow(record, scopes)
            else:
                record = await self._start_agent(record, scopes)
        return record

    def _resolve_context_refs(self, refs: tuple[str, ...], *, context: Any) -> list[dict[str, Any]]:
        """引用错误映射为可见输入／授权错误，不暴露底层对象或存储细节。"""
        try:
            return resolve_context_refs(
                refs,
                context=context,
                conversations=self.conversation_repository,
                artifacts=self.artifact_service,
            )
        except PermissionError as exc:
            raise DelegationAuthorizationError("context reference is not authorized") from exc
        except (ValueError, LookupError) as exc:
            raise DelegationInputError("context reference is unavailable or invalid") from exc

    async def status(
        self,
        delegation_id: str,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str] | None = None,
    ) -> DelegationRecord:
        """查询 delegation 状态；必要时恢复子运行并同步最新进度。

        Args:
            delegation_id: delegation ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。
            scopes: 当前权限上界；None 使用原授权，显式空集合表示撤权。

        Returns:
            delegation 记录（已同步子运行最新状态）。

        """
        await asyncio.to_thread(self.execution.require_legacy)
        record = await asyncio.to_thread(
            self.repository.get_owned, delegation_id, tenant_id, subject_id
        )
        # 1. 已到终态（含 DELIVERED）直接返回。
        if record.status in {
            DelegationStatus.COMPLETED,
            DelegationStatus.REJECTED,
            DelegationStatus.FAILED,
            DelegationStatus.DELIVERED,
        }:
            return record
        # 仅使用原授权快照恢复，显式传入的当前权限只能进一步收窄。
        if not record.execution_snapshot:
            raise ExecutionConflict("delegation authorization snapshot is missing")
        recovery_scopes = snapshot_context(record.execution_snapshot, scopes).scopes
        if record.child_run_id is None:
            if record.kind is DelegationKind.WORKFLOW:
                record = await self._start_workflow(record, recovery_scopes)
            else:
                record = await self._start_agent(record, recovery_scopes)
        elif record.kind is DelegationKind.AGENT and record.child_server_run_id is None:
            record = await self._start_agent(record, recovery_scopes)
        # 3. 按子运行类型同步状态：Workflow 走工作流服务，Agent 查询 server。
        if record.kind is DelegationKind.WORKFLOW:
            child = await self.workflow_service.status(
                record.child_run_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                scopes=scopes,
            )
            return await self._sync_child_status(record, child)
        return await self._agent_status(record, scopes=scopes)

    async def resume(
        self,
        record: DelegationRecord,
        decision: ApprovalDecision,
        *,
        scopes: frozenset[str],
    ) -> DelegationRecord:
        """把审批决定转发给 Workflow 子运行并同步 delegation 状态。

        Args:
            record: 当前 delegation 记录。
            decision: 审批决定（approve/reject 及理由、参数 hash）。
            scopes: 调用方权限范围，用于子工作流审批鉴权。

        Returns:
            同步子状态后的 delegation 记录。

        Raises:
            DelegationConflict: 子运行不是 Workflow 或尚未启动，无法恢复。

        """
        await asyncio.to_thread(self.execution.require_legacy)
        if record.child_run_id is None:
            raise DelegationConflict("delegated child does not support approval resume")
        if record.kind is DelegationKind.AGENT:
            if not await self.interactions.resume_legacy(
                record.child_run_id,
                decision,
                tenant_id=record.tenant_id,
                subject_id=record.subject_id,
                scopes=scopes,
            ):
                raise DelegationConflict(
                    "child has no uniquely bound approval; use interaction response API"
                )
            return await self._agent_status(record)
        child = await self.workflow_service.resume(
            record.child_run_id,
            decision,
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            scopes=scopes,
        )
        return await self._sync_child_status(record, child)

    async def child_status(
        self,
        child_run_id: str,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str] | None = None,
    ) -> RunStatusResponse:
        """按子运行 ID 查询 delegation 子运行的最新状态。

        Args:
            child_run_id: 子业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。
            scopes: 当前认证范围；缺省的后台查询不领取尚未提交的用户回答。

        Returns:
            子运行的状态响应。

        Raises:
            RunNotFound: delegation 不存在或子运行尚未启动。

        """
        try:
            record = await asyncio.to_thread(
                self.repository.get_by_child_owned,
                child_run_id,
                tenant_id,
                subject_id,
            )
        except DelegationNotFound as exc:
            raise RunNotFound(str(exc)) from exc
        current = await self.status(
            record.delegation_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
        )
        if current.child_run_id is None or current.child_thread_id is None:
            raise RunNotFound("delegated child run has not started")
        return RunStatusResponse(
            run_id=current.child_run_id,
            thread_id=current.child_thread_id,
            status=current.status.value,
            output=current.output_payload,
            waiting_reason=(current.output_payload or {}).get("waiting_reason")
            if current.status is DelegationStatus.INTERRUPTED
            else None,
            pending_interactions=tuple(
                (current.output_payload or {}).get("pending_interactions", ())
            )
            if current.status is DelegationStatus.INTERRUPTED
            else (),
        )

    async def mark_delivered(self, record: DelegationRecord) -> DelegationRecord:
        """把 delegation 标记为已交付父运行，并在状态变化时写审计。

        Args:
            record: 待标记的 delegation 记录。

        Returns:
            更新后的 delegation 记录（状态为 DELIVERED）。

        """
        delivered, changed = await asyncio.to_thread(
            self.repository.set_status,
            record.delegation_id,
            DelegationStatus.DELIVERED,
        )
        if changed:
            await self._audit(
                delivered,
                AuditEventType.DELEGATION_DELIVERED,
                decision="delivered_to_parent",
            )
        return delivered

    async def latest_for_parent(
        self, parent_run_id: str, *, tenant_id: str, subject_id: str
    ) -> DelegationRecord | None:
        """查询父运行最近一条尚未交付的 delegation。

        Args:
            parent_run_id: 父业务 run ID。
            tenant_id: 租户 ID。
            subject_id: 主体 ID。

        Returns:
            未交付的 delegation 记录；不存在时为 None。

        """
        return await asyncio.to_thread(
            self.repository.latest_undelivered_for_parent,
            parent_run_id,
            tenant_id,
            subject_id,
        )

    async def reconcile_incomplete(self) -> tuple[str, ...]:
        """对账所有未交付的 delegation：逐条刷新状态直至子运行收敛。

        Returns:
            本次完成对账的 delegation ID 列表。

        """
        await asyncio.to_thread(self.execution.require_legacy)
        records = await asyncio.to_thread(self.repository.list_undelivered)
        reconciled: list[str] = []
        for record in records:
            # 1. 逐条触发状态同步（内部会恢复未启动的子运行）。
            await self.status(
                record.delegation_id,
                tenant_id=record.tenant_id,
                subject_id=record.subject_id,
            )
            reconciled.append(record.delegation_id)
        # 2. 返回已对账的 delegation ID 列表。
        return tuple(reconciled)

    @staticmethod
    def result(record: DelegationRecord) -> DelegationResult:
        """把到终态的 delegation 投影为可交付父运行的 DelegationResult。

        Args:
            record: delegation 记录。

        Returns:
            含子运行结果、输出与错误信息的投影对象。

        Raises:
            DelegationConflict: 子运行未启动或尚未到终态。

        """
        if record.child_run_id is None or record.status not in {
            DelegationStatus.COMPLETED,
            DelegationStatus.REJECTED,
            DelegationStatus.FAILED,
        }:
            raise DelegationConflict("delegation has no terminal child result")
        return DelegationResult(
            delegation_id=record.delegation_id,
            kind=record.kind,
            target_id=record.target_id,
            target_version=record.target_version,
            child_run_id=record.child_run_id,
            status=record.status.value,
            parent_run_id=record.parent_run_id,
            arguments_hash=record.arguments_hash,
            output=record.output_payload,
            error=record.error,
        )

    async def _start_workflow(
        self, record: DelegationRecord, scopes: frozenset[str]
    ) -> DelegationRecord:
        """以 delegation_id 为幂等键启动 Workflow 子运行并绑定映射。

        Args:
            record: 待启动的 delegation 记录。
            scopes: 调用方权限范围，用于工作流鉴权。

        Returns:
            绑定子运行后的 delegation 记录。

        Raises:
            WorkflowAuthorizationError: 缺少工作流所需权限范围。
            WorkflowInputError: 工作流入参不合法。

        """
        try:
            definition = self.workflow_service.catalog[(record.target_id, record.target_version)]
            if not record.execution_snapshot or record.execution_snapshot.get(
                "target_release"
            ) != self.workflow_service._release(definition):
                raise ExecutionConflict("pinned delegated Workflow release is unavailable")
            # 1. 以 delegation_id 为幂等键启动工作流。
            accepted = await self.workflow_service.start(
                WorkflowTarget(
                    workflow_id=record.target_id,
                    version=record.target_version,
                    arguments=record.arguments,
                ),
                tenant_id=record.tenant_id,
                subject_id=record.subject_id,
                scopes=scopes,
                idempotency_key=record.delegation_id,
                parent_snapshot=record.execution_snapshot,
                parent_run_id=record.parent_run_id,
            )
        except (WorkflowAuthorizationError, WorkflowInputError) as exc:
            # 2. 启动失败：先把 delegation 置为 FAILED，再向上抛出。
            await self._fail_start(record, str(exc))
            raise
        # 3. 读取工作流运行并绑定 child 映射与状态。
        workflow = await asyncio.to_thread(
            self.workflow_service.repository.get_owned,
            accepted.run_id,
            record.tenant_id,
            record.subject_id,
        )
        bound = await asyncio.to_thread(
            self.repository.bind_child,
            record.delegation_id,
            child_run_id=workflow.run_id,
            child_thread_id=workflow.thread_id,
            child_server_run_id=workflow.server_run_id,
            status=_delegation_status(workflow.status.value),
        )
        # 4. 写 STARTED 审计。
        await self._audit(bound, AuditEventType.DELEGATION_STARTED, decision="child_started")
        return bound

    async def _start_agent(
        self,
        record: DelegationRecord,
        scopes: frozenset[str],
    ) -> DelegationRecord:
        """用冻结输入、必要引用和独立线程启动子 Agent，不装入根 Journal。"""
        if not record.execution_snapshot:
            raise ExecutionConflict("delegation authorization snapshot is missing")
        profile = self.agent_profiles.resolve(record.target_id, record.target_version)
        verify_agent_snapshot(profile, record.execution_snapshot.get("target_release", {}))
        original = snapshot_context(record.execution_snapshot)
        effective = snapshot_context(record.execution_snapshot, scopes)
        self._require_scopes(effective.scopes, profile.required_scopes)
        prepared = await asyncio.to_thread(
            self.repository.prepare_agent_child, record.delegation_id
        )
        context = original.model_copy(
            update={
                "run_id": prepared.child_run_id,
                "root_run_id": original.root_run_id or record.parent_run_id,
                "parent_run_id": record.parent_run_id,
                "delegation_id": record.delegation_id,
            }
        )
        snapshot = agent_snapshot(
            profile, context, thread_id=prepared.child_thread_id, input_hash=record.arguments_hash
        )
        snapshot["resolved_context"] = record.execution_snapshot.get("resolved_context", [])
        try:
            execution = await asyncio.to_thread(self.execution.get, prepared.child_run_id)
        except ExecutionConflict:
            execution = await asyncio.to_thread(
                self.execution.register,
                prepared.child_run_id,
                snapshot,
                root_run_id=context.root_run_id,
            )
        verify_agent_snapshot(profile, execution["snapshot"])
        arguments = record.arguments.get("arguments", {})
        if profile.input_schema is not None:
            try:
                arguments = profile.input_schema.model_validate(arguments).model_dump(mode="json")
            except ValidationError as exc:
                missing = [
                    str(error["loc"][0]) for error in exc.errors() if error["type"] == "missing"
                ]
                if len(missing) != len(exc.errors()) or profile.output_schema is None:
                    return await self._transition(
                        prepared, DelegationStatus.FAILED, error="invalid delegated input"
                    )
                clarification = {
                    "outcome": "needs_clarification",
                    "missing_fields": missing,
                    "question": "Please provide: " + ", ".join(missing),
                }
                try:
                    output = profile.output_schema.model_validate(clarification).model_dump(
                        mode="json"
                    )
                except ValidationError:
                    return await self._transition(
                        prepared,
                        DelegationStatus.FAILED,
                        error="Agent output schema cannot represent missing input",
                    )
                # 低成本提槽：旧 child 已完成；根会话提问后必须创建新 Turn 和新 child。
                return await self._transition(prepared, DelegationStatus.COMPLETED, output=output)
        await self.client.create_thread(prepared.child_thread_id)
        task = {
            "task": record.arguments["task"],
            "arguments": arguments,
            "authorized_context": execution["snapshot"].get("resolved_context", []),
        }
        submit_context = snapshot_context(execution["snapshot"], scopes)
        server = await self.operations.submit(
            prepared.child_run_id,
            "start",
            thread_id=prepared.child_thread_id,
            assistant_id=execution["snapshot"]["assistant_id"],
            input={"messages": [{"role": "user", "content": json.dumps(task, ensure_ascii=False)}]},
            context=submit_context.model_dump(mode="json"),
            metadata={
                **submit_context.trace_metadata(),
                "application_run_id": prepared.child_run_id,
                "target_kind": "agent_delegation",
                "agent_id": profile.agent_id,
                "agent_profile_version": profile.version,
                "parent_run_id": record.parent_run_id,
                "delegation_id": record.delegation_id,
            },
        )
        if server is None:
            return prepared
        bound = await asyncio.to_thread(
            self.repository.bind_child,
            prepared.delegation_id,
            child_run_id=prepared.child_run_id,
            child_thread_id=prepared.child_thread_id,
            child_server_run_id=server.run_id,
            status=DelegationStatus.RUNNING,
        )
        if record.child_server_run_id is None:
            await self._audit(bound, AuditEventType.DELEGATION_STARTED, decision="child_started")
        return bound

    async def _agent_status(
        self, record: DelegationRecord, *, scopes: frozenset[str] | None = None
    ) -> DelegationRecord:
        """先分类中断，再按 Profile 声明的 state 字段校验领域结果。"""
        if record.child_thread_id is None or record.child_server_run_id is None:
            return record
        await self.interactions.reconcile_owner(record.child_run_id, scopes=scopes)
        uncertain = await self.operations.reconcile(record.child_run_id)
        execution = await asyncio.to_thread(self.execution.get, record.child_run_id)
        if uncertain:
            return await self._transition(
                record,
                DelegationStatus.INTERRUPTED,
                output={"waiting_reason": "submission_uncertain", "pending_interactions": []},
            )
        server_id = execution["server_run_id"]
        server = await self.client.get_run(thread_id=record.child_thread_id, run_id=server_id)
        observation = observe_run(server)
        if observation.kind == "completed":
            raw = await self.client.join_run(thread_id=record.child_thread_id, run_id=server_id)
            observation = observe_run(raw)
            if observation.kind == "completed":
                profile = self.agent_profiles.resolve(record.target_id, record.target_version)
                execution = await asyncio.to_thread(self.execution.get, record.child_run_id)
                verify_agent_snapshot(profile, execution["snapshot"])
                try:
                    if profile.output_schema is not None:
                        candidate = raw.get(profile.output_state_key)
                        output = profile.output_schema.model_validate(candidate).model_dump(
                            mode="json"
                        )
                    else:
                        message = _final_assistant_content(raw)
                        if message is None:
                            raise ValueError("missing final Agent result")
                        output = {"message": message}
                except (ValueError, TypeError):
                    return await self._transition(
                        record,
                        DelegationStatus.FAILED,
                        error="invalid domain Agent structured result",
                    )
                return await self._transition(record, DelegationStatus.COMPLETED, output=output)
        if observation.kind == "failed":
            return await self._transition(
                record, DelegationStatus.FAILED, error="domain Agent child run failed"
            )
        if observation.kind in {"hitl", "interaction"} and observation.interrupt_id:
            from datetime import timedelta

            interaction = await self.interactions.observe_agent(
                record.child_run_id,
                observation,
                server_run_id=server_id,
                expires_at=self.interactions.clock() + timedelta(seconds=900),
                checkpoint_id=(server.get("checkpoint") or {}).get("checkpoint_id"),
            )
            public = await self.interactions.public(interaction)
            return await self._transition(
                record,
                DelegationStatus.INTERRUPTED,
                output={"waiting_reason": waiting_reason(public), "pending_interactions": [public]},
            )
        if observation.kind != "running":
            # 未声明／多位置中断仍保持可见不支持状态，不能广播回答。
            return await self._transition(
                record,
                DelegationStatus.INTERRUPTED,
                error="unsupported_child_interaction; cancel or use clarification outcome",
            )
        return await self._transition(record, DelegationStatus.RUNNING)

    async def _sync_child_status(
        self, record: DelegationRecord, child: RunStatusResponse
    ) -> DelegationRecord:
        """把子运行状态响应同步为 delegation 状态转移。

        Args:
            record: delegation 记录。
            child: 子运行最新状态响应。

        Returns:
            状态转移后的 delegation 记录。

        """
        status = _delegation_status(child.status)
        error = "delegated Workflow failed" if status is DelegationStatus.FAILED else None
        output = child.output
        if status is DelegationStatus.INTERRUPTED:
            output = {
                **(child.output or {}),
                "waiting_reason": child.waiting_reason,
                "pending_interactions": list(child.pending_interactions),
            }
        return await self._transition(record, status, output=output, error=error)

    async def _transition(
        self,
        record: DelegationRecord,
        status: DelegationStatus,
        *,
        output: dict[str, Any] | list[Any] | None = None,
        error: str | None = None,
    ) -> DelegationRecord:
        """落地 delegation 状态转移，并在状态实际变化时写对应审计事件。

        Args:
            record: delegation 记录。
            status: 目标状态。
            output: 终态输出载荷；非字典（如列表）时忽略。
            error: 失败原因描述。

        Returns:
            更新后的 delegation 记录。

        """
        normalized_output = output if isinstance(output, dict) else None
        updated, changed = await asyncio.to_thread(
            self.repository.set_status,
            record.delegation_id,
            status,
            output_payload=normalized_output,
            error=error,
        )
        if changed:
            event = {
                DelegationStatus.INTERRUPTED: AuditEventType.DELEGATION_INTERRUPTED,
                DelegationStatus.COMPLETED: AuditEventType.DELEGATION_COMPLETED,
                DelegationStatus.REJECTED: AuditEventType.DELEGATION_COMPLETED,
                DelegationStatus.FAILED: AuditEventType.DELEGATION_FAILED,
            }.get(status)
            if event is not None:
                await self._audit(updated, event, decision=status.value)
        return updated

    async def _fail_start(self, record: DelegationRecord, error: str) -> None:
        """把启动失败的 delegation 置为 FAILED 并记录错误信息。

        Args:
            record: delegation 记录。
            error: 失败原因描述。

        """
        await self._transition(record, DelegationStatus.FAILED, error=error)

    def _resolve(
        self, handoff: HandoffRequest, scopes: frozenset[str], parent_snapshot: dict[str, Any]
    ) -> tuple[DelegationKind, str, str, dict[str, Any]]:
        """解析 handoff 目标并校验权限，返回四元组供落库使用。

        Args:
            handoff: typed handoff 请求。
            scopes: 调用方权限范围。
            parent_snapshot: 父 Profile 的工具版本绑定，不从 latest 选择。

        Returns:
            （目标类型, 目标 ID, 目标版本, 归一化后参数）四元组。

        Raises:
            DelegationAuthorizationError: 缺少目标所需权限范围。
            DelegationInputError: 目标不存在、不可委托或参数非法。

        """
        try:
            target = (
                handoff.workflow_id if isinstance(handoff, WorkflowHandoff) else handoff.agent_id
            )
            tool_name = f"delegate_{handoff.kind.value}__{target}"
            versions = {
                ref["tool_id"]: ref["version"]
                for ref in parent_snapshot["profile"]["allowed_tools"]
            }
            pinned_version = versions.get(tool_name)
            if pinned_version is None:
                raise DelegationInputError("handoff target is not bound in the parent release")
            if getattr(handoff, "target_version", None) not in {None, pinned_version}:
                raise DelegationInputError(
                    "handoff target version differs from parent tool binding"
                )
            # Workflow handoff：解析已发布定义、校验权限并归一化入参。
            if isinstance(handoff, WorkflowHandoff):
                definition = self.workflow_service.catalog.resolve(
                    handoff.workflow_id, pinned_version
                )
                self._require_scopes(scopes, definition.required_scopes)
                return (
                    DelegationKind.WORKFLOW,
                    definition.workflow_id,
                    definition.version,
                    definition.normalize_input(handoff.arguments),
                )
            # Agent handoff：目标 Profile 必须显式允许被委托（delegatable）。
            profile = self.agent_profiles.resolve(handoff.agent_id, pinned_version)
            if not profile.delegatable:
                raise DelegationInputError("AgentProfile is not available for delegation")
            self._require_scopes(scopes, profile.required_scopes)
            arguments = AgentDelegationInput(
                task=handoff.task,
                context_refs=handoff.context_refs,
                arguments=getattr(handoff, "arguments", {}),
            ).model_dump(mode="json")
            return DelegationKind.AGENT, profile.agent_id, profile.version, arguments
        except DelegationAuthorizationError:
            raise
        except Exception as exc:
            # 除授权错误外，其余异常统一包装为入参错误。
            raise DelegationInputError(str(exc)) from exc

    @staticmethod
    def _verify_parent(
        handoff: HandoffRequest,
        *,
        parent_run_id: str,
        parent_turn_id: str,
        conversation_id: str,
    ) -> None:
        """校验 handoff 声明的父 run/turn/会话引用与当前 Turn 完全一致。

        Args:
            handoff: typed handoff 请求。
            parent_run_id: 父业务 run ID。
            parent_turn_id: 父业务 Turn ID。
            conversation_id: 所属会话 ID。

        Raises:
            DelegationInputError: 任一父引用不匹配。

        """
        if (
            handoff.parent_run_id != parent_run_id
            or handoff.parent_turn_id != parent_turn_id
            or handoff.conversation_id != conversation_id
        ):
            raise DelegationInputError("handoff parent references do not match the owned turn")

    @staticmethod
    def _require_scopes(granted: frozenset[str], required: frozenset[str]) -> None:
        """校验授予权限范围覆盖所需范围（通配 "*" 直接放行）。

        Args:
            granted: 调用方被授予的权限范围。
            required: 目标所需的权限范围。

        Raises:
            DelegationAuthorizationError: 所需范围未被覆盖。

        """
        if "*" not in granted and not required.issubset(granted):
            raise DelegationAuthorizationError("required delegation scope is missing")

    async def _audit(
        self, record: DelegationRecord, event: AuditEventType, *, decision: str
    ) -> None:
        """写一条 delegation 审计记录（策略版本固定为 delegation-policy/1.0.0）。

        Args:
            record: delegation 记录，提供主体与父子引用字段。
            event: 审计事件类型。
            decision: 审计决策描述（如 requested、child_started）。

        """
        await asyncio.to_thread(
            self.audit.append,
            AuditRecord(
                event_type=event,
                tenant_id=record.tenant_id,
                subject_id=record.subject_id,
                conversation_id=record.conversation_id,
                turn_id=record.parent_turn_id,
                run_id=record.parent_run_id,
                resource_type="delegation",
                resource_id=record.target_id,
                resource_version=record.target_version,
                action=record.kind.value,
                decision=decision,
                policy_version="delegation-policy/1.0.0",
                payload_hash=record.arguments_hash,
                evidence_refs=(record.delegation_id,),
                metadata={"child_run_id": record.child_run_id},
            ),
        )


def extract_handoff_interrupt(value: Mapping[str, Any]) -> HandoffRequest | None:
    """从 server 响应中提取首个合法的 typed handoff（schema_version=1）。

    Args:
        value: server 运行详情（中断位于 "interrupts" 或 "__interrupt__" 键）。

    Returns:
        解析出的 WorkflowHandoff 或 AgentHandoff；无中断时为 None。

    Raises:
        DelegationInputError: 中断载荷携带了不合法的 typed handoff。

    """
    observed = observe_run(value)
    if observed.kind == "unsupported":
        raise DelegationInputError("unsupported or ambiguous interrupt batch")
    return observed.handoff


def delegation_projection(record: DelegationRecord) -> dict[str, Any]:
    """把 delegation 记录裁剪为对外可见的字段投影。

    Args:
        record: delegation 记录。

    Returns:
        含标识、目标、子运行与状态输出的可序列化字典。

    """
    return {
        "delegation_id": record.delegation_id,
        "kind": record.kind.value,
        "target_id": record.target_id,
        "target_version": record.target_version,
        "child_run_id": record.child_run_id,
        "status": record.status.value,
        "execution_status": record.execution_status,
        "delivery_status": record.delivery_status,
        "output": record.output_payload,
        "error": record.error,
    }


def _delegation_status(status: str) -> DelegationStatus:
    """把子运行状态字符串映射为 delegation 状态（未知值回落 PENDING）。

    Args:
        status: 子运行状态字符串（工作流或 server 状态值，"success" 视为完成）。

    Returns:
        对应的 delegation 状态。

    """
    return {
        WorkflowRunStatus.ACCEPTED.value: DelegationStatus.PENDING,
        WorkflowRunStatus.PENDING.value: DelegationStatus.PENDING,
        WorkflowRunStatus.RUNNING.value: DelegationStatus.RUNNING,
        WorkflowRunStatus.INTERRUPTED.value: DelegationStatus.INTERRUPTED,
        WorkflowRunStatus.COMPLETED.value: DelegationStatus.COMPLETED,
        WorkflowRunStatus.REJECTED.value: DelegationStatus.REJECTED,
        WorkflowRunStatus.FAILED.value: DelegationStatus.FAILED,
        "success": DelegationStatus.COMPLETED,
    }.get(status, DelegationStatus.PENDING)


def _final_assistant_content(output: Mapping[str, Any]) -> str | None:
    """从子运行输出的消息列表中取最后一条 AI/assistant 消息的文本内容。

    Args:
        output: 运行输出映射（含 "messages" 键时生效）。

    Returns:
        最终回复文本；非字符串内容序列化为 JSON，找不到时为 None。

    """
    messages = output.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return (
                message.content
                if isinstance(message.content, str)
                else json.dumps(message.content, default=str)
            )
        if isinstance(message, Mapping) and message.get("type") in {"ai", "assistant"}:
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content, default=str)
    return None
