"""编排父运行向工作流或专业 Agent 的可恢复委派。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from financeclaw.kernel import (
    ApprovalDecision,
    ExecutionContext,
    RunStatusResponse,
    WorkflowTarget,
)
from financeclaw.modules.audit import AuditEventType, AuditRecord, AuditRepository
from financeclaw.modules.delegation import (
    HANDOFF_ADAPTER,
    AgentDelegationInput,
    DelegationConflict,
    DelegationKind,
    DelegationNotFound,
    DelegationRecord,
    DelegationRepository,
    DelegationResult,
    DelegationStatus,
    HandoffRequest,
    WorkflowHandoff,
)
from financeclaw.modules.workflows import WorkflowRunStatus
from financeclaw.orchestration.agents import AgentProfileCatalog

from .ports import AgentServerClient
from .run_service import RunNotFound
from .workflow_service import WorkflowAuthorizationError, WorkflowInputError, WorkflowService


class DelegationInputError(ValueError):
    """定义委派输入Error。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


class DelegationAuthorizationError(PermissionError):
    """定义委派AuthorizationError。

    适用场景：
        用于把该失败条件跨层传递，并在接口边界转换为稳定错误。
    """

    pass


class DelegationService:
    """协调父运行与子 Agent/工作流之间的创建、轮询、恢复和结果交付。

    适用场景：
        用于应用用例需要跨仓储、外部端口或领域策略协调一致结果的场景。

    属性：
        client: 负责与外部 Agent Server 或供应商通信的端口实现。
        repository: 负责领域状态读写和事务一致性的仓储。
        workflow_service: 负责启动、查询和恢复确定性工作流的应用服务。
        agent_profiles: 可按稳定标识和版本解析 Agent 配置的只读目录。
        audit: 记录授权、执行和状态变化的审计仓储。
    """

    def __init__(
        self,
        client: AgentServerClient,
        repository: DelegationRepository,
        workflow_service: WorkflowService,
        agent_profiles: AgentProfileCatalog,
        audit: AuditRepository,
    ) -> None:
        """注入并保存委派Service所需的协作对象，同时校验构造期不变量。"""
        self.client = client
        self.repository = repository
        self.workflow_service = workflow_service
        self.agent_profiles = agent_profiles
        self.audit = audit

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
    ) -> DelegationRecord:
        """解析并授权委派目标，幂等创建委派记录，再启动或复用对应子运行。"""
        self._verify_parent(
            handoff,
            parent_run_id=parent_run_id,
            parent_turn_id=parent_turn_id,
            conversation_id=conversation_id,
        )
        kind, target_id, target_version, arguments = self._resolve(handoff, scopes)
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
        )
        if created:
            await self._audit(
                record,
                AuditEventType.DELEGATION_REQUESTED,
                decision="requested",
            )
        if record.child_run_id is None:
            if record.kind is DelegationKind.WORKFLOW:
                record = await self._start_workflow(record, scopes)
            else:
                record = await self._start_agent(record, scopes)
        return record

    async def status(
        self, delegation_id: str, *, tenant_id: str, subject_id: str
    ) -> DelegationRecord:
        """读取委派及其子运行状态，必要时推进状态机并持久化变化。"""
        record = await asyncio.to_thread(
            self.repository.get_owned, delegation_id, tenant_id, subject_id
        )
        if record.status in {
            DelegationStatus.COMPLETED,
            DelegationStatus.REJECTED,
            DelegationStatus.FAILED,
            DelegationStatus.DELIVERED,
        }:
            return record
        if record.child_run_id is None:
            recovery_scopes = frozenset({"*"})
            if record.kind is DelegationKind.WORKFLOW:
                record = await self._start_workflow(record, recovery_scopes)
            else:
                record = await self._start_agent(record, recovery_scopes)
        elif record.kind is DelegationKind.AGENT and record.child_server_run_id is None:
            record = await self._start_agent(record, frozenset({"*"}))
        if record.kind is DelegationKind.WORKFLOW:
            child = await self.workflow_service.status(
                record.child_run_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
            )
            return await self._sync_child_status(record, child)
        return await self._agent_status(record)

    async def resume(
        self,
        record: DelegationRecord,
        decision: ApprovalDecision,
        *,
        scopes: frozenset[str],
    ) -> DelegationRecord:
        """把审批决定转交子工作流或 Agent，并同步委派状态。"""
        if record.kind is not DelegationKind.WORKFLOW or record.child_run_id is None:
            raise DelegationConflict("delegated child does not support approval resume")
        child = await self.workflow_service.resume(
            record.child_run_id,
            decision,
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            scopes=scopes,
        )
        return await self._sync_child_status(record, child)

    async def child_status(
        self, child_run_id: str, *, tenant_id: str, subject_id: str
    ) -> RunStatusResponse:
        """根据委派目标类型查询子 Agent 或工作流的统一运行状态。"""
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
        )
        if current.child_run_id is None or current.child_thread_id is None:
            raise RunNotFound("delegated child run has not started")
        return RunStatusResponse(
            run_id=current.child_run_id,
            thread_id=current.child_thread_id,
            status=current.status.value,
            output=current.output_payload,
        )

    async def mark_delivered(self, record: DelegationRecord) -> DelegationRecord:
        """以幂等方式标记委派Service的状态。"""
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
        """读取父运行最近一个尚未完成结果交付的委派。"""
        return await asyncio.to_thread(
            self.repository.latest_undelivered_for_parent,
            parent_run_id,
            tenant_id,
            subject_id,
        )

    async def reconcile_incomplete(self) -> tuple[str, ...]:
        """扫描未完成委派并与子运行对账，返回本次成功推进的委派标识。"""
        records = await asyncio.to_thread(self.repository.list_undelivered)
        reconciled: list[str] = []
        for record in records:
            await self.status(
                record.delegation_id,
                tenant_id=record.tenant_id,
                subject_id=record.subject_id,
            )
            reconciled.append(record.delegation_id)
        return tuple(reconciled)

    @staticmethod
    def result(record: DelegationRecord) -> DelegationResult:
        """返回已完成委派的规范化结果；未完成或失败时抛出状态冲突。"""
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
            output=record.output_payload,
            error=record.error,
        )

    async def _start_workflow(
        self, record: DelegationRecord, scopes: frozenset[str]
    ) -> DelegationRecord:
        """校验输入后启动委派Service，返回可供后续查询的记录。"""
        try:
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
            )
        except (WorkflowAuthorizationError, WorkflowInputError) as exc:
            await self._fail_start(record, str(exc))
            raise
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
        await self._audit(bound, AuditEventType.DELEGATION_STARTED, decision="child_started")
        return bound

    async def _start_agent(
        self, record: DelegationRecord, scopes: frozenset[str]
    ) -> DelegationRecord:
        """校验输入后启动委派Service，返回可供后续查询的记录。"""
        profile = self.agent_profiles.resolve(record.target_id, record.target_version)
        self._require_scopes(scopes, profile.required_scopes)
        prepared = await asyncio.to_thread(
            self.repository.prepare_agent_child, record.delegation_id
        )
        if prepared.child_run_id is None or prepared.child_thread_id is None:
            raise DelegationConflict("Agent child identity was not prepared")
        await self.client.create_thread(prepared.child_thread_id)
        server_run = await self.client.find_run(
            thread_id=prepared.child_thread_id,
            application_run_id=prepared.child_run_id,
        )
        if server_run is None:
            context = ExecutionContext(
                tenant_id=prepared.tenant_id,
                subject_id=prepared.subject_id,
                scopes=scopes,
                conversation_id=prepared.conversation_id,
                turn_id=prepared.parent_turn_id,
                run_id=prepared.child_run_id,
            )
            server_run = await self.client.create_run(
                thread_id=prepared.child_thread_id,
                assistant_id=profile.agent_id,
                input={"messages": [{"role": "user", "content": prepared.arguments["task"]}]},
                context=context.model_dump(mode="json"),
                metadata={
                    **context.trace_metadata(),
                    "application_run_id": prepared.child_run_id,
                    "target_kind": "agent_delegation",
                    "agent_id": profile.agent_id,
                    "agent_profile_version": profile.version,
                    "parent_run_id": prepared.parent_run_id,
                    "delegation_id": prepared.delegation_id,
                },
            )
        bound = await asyncio.to_thread(
            self.repository.bind_child,
            prepared.delegation_id,
            child_run_id=prepared.child_run_id,
            child_thread_id=prepared.child_thread_id,
            child_server_run_id=server_run.run_id,
            status=_delegation_status(server_run.status),
        )
        await self._audit(bound, AuditEventType.DELEGATION_STARTED, decision="child_started")
        return bound

    async def _agent_status(self, record: DelegationRecord) -> DelegationRecord:
        """读取 Agent Server 子运行并映射为委派生命周期状态。"""
        if record.child_thread_id is None or record.child_server_run_id is None:
            return record
        server = await self.client.get_run(
            thread_id=record.child_thread_id,
            run_id=record.child_server_run_id,
        )
        status = str(server.get("status", record.status.value))
        if status in {"success", "completed"}:
            raw = await self.client.join_run(
                thread_id=record.child_thread_id,
                run_id=record.child_server_run_id,
            )
            output = {"message": _final_assistant_content(raw) or ""}
            return await self._transition(
                record,
                DelegationStatus.COMPLETED,
                output=output,
            )
        if status in {"error", "failed"}:
            return await self._transition(
                record,
                DelegationStatus.FAILED,
                error="domain Agent child run failed",
            )
        if status == "interrupted":
            return await self._transition(record, DelegationStatus.INTERRUPTED)
        return await self._transition(record, _delegation_status(status))

    async def _sync_child_status(
        self, record: DelegationRecord, child: RunStatusResponse
    ) -> DelegationRecord:
        """把统一子运行响应映射并写入委派状态机。"""
        status = _delegation_status(child.status)
        error = "delegated Workflow failed" if status is DelegationStatus.FAILED else None
        return await self._transition(record, status, output=child.output, error=error)

    async def _transition(
        self,
        record: DelegationRecord,
        status: DelegationStatus,
        *,
        output: dict[str, Any] | list[Any] | None = None,
        error: str | None = None,
    ) -> DelegationRecord:
        """使用仓储乐观锁推进委派状态，并追加对应审计事件。"""
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
        """在子运行启动失败时将委派标记失败并记录原因。"""
        await self._transition(record, DelegationStatus.FAILED, error=error)

    def _resolve(
        self, handoff: HandoffRequest, scopes: frozenset[str]
    ) -> tuple[DelegationKind, str, str, dict[str, Any]]:
        """解析委派类型、固定目标版本、校验输入与所需权限。"""
        try:
            if isinstance(handoff, WorkflowHandoff):
                definition = self.workflow_service.catalog.resolve(handoff.workflow_id)
                self._require_scopes(scopes, definition.required_scopes)
                return (
                    DelegationKind.WORKFLOW,
                    definition.workflow_id,
                    definition.version,
                    definition.normalize_input(handoff.arguments),
                )
            profile = self.agent_profiles.resolve(handoff.agent_id)
            if not profile.delegatable:
                raise DelegationInputError("AgentProfile is not available for delegation")
            self._require_scopes(scopes, profile.required_scopes)
            arguments = AgentDelegationInput(
                task=handoff.task,
                context_refs=handoff.context_refs,
            ).model_dump(mode="json")
            return DelegationKind.AGENT, profile.agent_id, profile.version, arguments
        except DelegationAuthorizationError:
            raise
        except Exception as exc:
            raise DelegationInputError(str(exc)) from exc

    @staticmethod
    def _verify_parent(
        handoff: HandoffRequest,
        *,
        parent_run_id: str,
        parent_turn_id: str,
        conversation_id: str,
    ) -> None:
        """校验父运行、轮次、会话、租户和主体引用保持一致。"""
        if (
            handoff.parent_run_id != parent_run_id
            or handoff.parent_turn_id != parent_turn_id
            or handoff.conversation_id != conversation_id
        ):
            raise DelegationInputError("handoff parent references do not match the owned turn")

    @staticmethod
    def _require_scopes(granted: frozenset[str], required: frozenset[str]) -> None:
        """比较所需与已有权限域，缺失任一权限时拒绝操作。"""
        if "*" not in granted and not required.issubset(granted):
            raise DelegationAuthorizationError("required delegation scope is missing")

    async def _audit(
        self, record: DelegationRecord, event: AuditEventType, *, decision: str
    ) -> None:
        """构造不可变审计事件并写入审计仓储。"""
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
    """从 LangGraph 中断载荷中识别并校验委派请求。"""
    raw_items = value.get("interrupts") or value.get("__interrupt__") or ()
    if isinstance(raw_items, Mapping):
        raw_items = (raw_items,)
    for item in raw_items if isinstance(raw_items, (list, tuple)) else ():
        raw = getattr(item, "value", None)
        if raw is None and isinstance(item, Mapping):
            raw = item.get("value", item)
        if not isinstance(raw, Mapping):
            continue
        if raw.get("schema_version") != 1 or "handoff_id" not in raw:
            continue
        try:
            return HANDOFF_ADAPTER.validate_python(raw)
        except ValidationError as exc:
            raise DelegationInputError("Agent returned an invalid typed handoff") from exc
    return None


def delegation_projection(record: DelegationRecord) -> dict[str, Any]:
    """把委派记录投影为可放入运行响应的公开结构。"""
    return {
        "delegation_id": record.delegation_id,
        "kind": record.kind.value,
        "target_id": record.target_id,
        "target_version": record.target_version,
        "child_run_id": record.child_run_id,
        "status": record.status.value,
        "output": record.output_payload,
        "error": record.error,
    }


def _delegation_status(status: str) -> DelegationStatus:
    """把子运行状态字符串归一化为委派状态枚举。"""
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
    """从服务端输出消息中提取最后一条助手文本。"""
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
