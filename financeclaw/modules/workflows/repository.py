"""工作流运行与审批的持久化仓储：Protocol 接口与 SQLAlchemy 实现。

BFF 借助本仓储永久保存业务 run、thread 与 server run 的映射、输入哈希、
审批决定与制品引用，并为审批恢复执行提供复验依据。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    WorkflowApproval,
    WorkflowApprovalStatus,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStatus,
)
from .tables import WorkflowApprovalRow, WorkflowRunRow


class WorkflowNotFound(LookupError):
    """目标运行或审批不存在时抛出的查找异常。

    使用场景：
        按标识读取不到记录、或记录不属于当前租户与主体时抛出。
    """

    pass


class WorkflowConflict(RuntimeError):
    """运行或审批状态发生非法变更时抛出的运行时异常。

    使用场景：
        终态被改写、server run 重复绑定或审批检查点漂移时抛出。
    """

    pass


class WorkflowIdempotencyConflict(RuntimeError):
    """客户端幂等键被用于不同请求时抛出的运行时异常。

    使用场景：
        同一幂等键再次到达但请求指纹或主体不同时抛出，
        提示调用方更换幂等键，而不是复用旧运行。
    """

    pass


class WorkflowRepository(Protocol):
    """工作流运行与审批仓储的持久化契约。

    使用场景：
        应用层依赖本接口读写运行与审批事实，具体实现可替换为
        SQLAlchemy 仓储或测试用假仓储。
    """

    def begin_run(
        self,
        *,
        definition: WorkflowDefinition,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        arguments_hash: str,
        request_fingerprint: str,
        input_payload: dict[str, Any],
    ) -> tuple[WorkflowRun, bool]:
        """按幂等键创建运行登记，或在请求指纹一致时复用既有运行。

        Returns:
            二元组：（运行记录，是否复用了既有记录）。

        """
        ...

    def get_owned(self, run_id: str, tenant_id: str, subject_id: str) -> WorkflowRun:
        """读取属于指定租户与主体的运行记录。

        Raises:
            WorkflowNotFound: 运行不存在或不属于该主体。

        """
        ...

    def bind_server_run(self, run_id: str, server_run_id: str, status: str) -> WorkflowRun:
        """把运行绑定到 Agent Server 的 server run 并归一化同步状态。

        Raises:
            WorkflowNotFound: 运行不存在。
            WorkflowConflict: 已绑定到其他 server run。

        """
        ...

    def set_status(
        self,
        run_id: str,
        status: WorkflowRunStatus,
        *,
        output_payload: dict[str, Any] | None = None,
        artifact_refs: tuple[str, ...] = (),
    ) -> tuple[WorkflowRun, bool]:
        """更新运行状态，可选写入输出与制品引用。

        Returns:
            二元组：（更新后的运行，状态是否发生变化）。

        """
        ...

    def list_incomplete(self) -> tuple[WorkflowRun, ...]:
        """列出全部未进入终态的运行，按启动时间升序。"""
        ...

    def ensure_approval(self, approval: WorkflowApproval) -> tuple[WorkflowApproval, bool]:
        """登记审批请求；同一运行的同一检查点幂等复用。

        Returns:
            二元组：（审批记录，是否为新创建）。

        """
        ...

    def get_approval(self, run_id: str) -> WorkflowApproval:
        """读取指定运行最近一次审批请求。

        Raises:
            WorkflowNotFound: 不存在审批记录。

        """
        ...

    def decide_approval(
        self,
        approval_id: str,
        *,
        status: WorkflowApprovalStatus,
        decided_by: str,
        reason: str | None,
        decided_at: datetime,
    ) -> tuple[WorkflowApproval, bool]:
        """落成审批决定；重复同一决定时幂等返回。

        Returns:
            二元组：（审批记录，是否为本次新决定）。

        """
        ...


def _run(row: WorkflowRunRow) -> WorkflowRun:
    """把运行 ORM 行转换为领域模型。"""
    return WorkflowRun(
        run_id=row.run_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        workflow_id=row.workflow_id,
        workflow_version=row.workflow_version,
        assistant_id=row.assistant_id,
        deployment_revision=row.deployment_revision,
        model_profile_id=row.model_profile_id,
        run_timeout_seconds=row.run_timeout_seconds,
        approval_timeout_seconds=row.approval_timeout_seconds,
        thread_id=row.thread_id,
        server_run_id=row.server_run_id,
        client_idempotency_key=row.client_idempotency_key,
        arguments_hash=row.arguments_hash,
        request_fingerprint=row.request_fingerprint,
        input_payload=row.input_payload,
        output_payload=row.output_payload,
        artifact_refs=tuple(row.artifact_refs),
        status=WorkflowRunStatus(row.status),
        started_at=row.started_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _approval(row: WorkflowApprovalRow) -> WorkflowApproval:
    """把审批 ORM 行转换为领域模型。"""
    return WorkflowApproval(
        approval_id=row.approval_id,
        run_id=row.run_id,
        tenant_id=row.tenant_id,
        subject_id=row.subject_id,
        approval_point=row.approval_point,
        arguments_hash=row.arguments_hash,
        requested_action=row.requested_action,
        request_payload=row.request_payload,
        allowed_decisions=tuple(row.allowed_decisions),
        required_scope=row.required_scope,
        status=WorkflowApprovalStatus(row.status),
        requested_at=row.requested_at,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        decision_reason=row.decision_reason,
    )


class SqlAlchemyWorkflowRepository:
    """基于 SQLAlchemy 的工作流仓储实现。

    使用场景：
        在 BFF 与应用服务中持久化工作流运行与审批事实；
        每个写方法都在独立事务中执行，读方法使用短会话。

    Attributes:
        _sessions: SQLAlchemy 会话工厂，写操作使用事务作用域。

    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        """注入 SQLAlchemy 会话工厂。

        Args:
            sessions: 会话工厂，写操作将使用其事务作用域。

        """
        self._sessions = sessions

    def begin_run(
        self,
        *,
        definition: WorkflowDefinition,
        tenant_id: str,
        subject_id: str,
        idempotency_key: str,
        arguments_hash: str,
        request_fingerprint: str,
        input_payload: dict[str, Any],
    ) -> tuple[WorkflowRun, bool]:
        """按幂等键创建运行登记，或在指纹一致时复用既有运行。

        Returns:
            二元组：（运行记录，是否复用了既有记录）。

        Raises:
            WorkflowIdempotencyConflict: 幂等键已被不同指纹或主体使用。

        """
        with self._sessions.begin() as session:
            # 1. 幂等查询：同一租户、流程、版本与幂等键的既有运行。
            existing = session.scalar(
                select(WorkflowRunRow).where(
                    WorkflowRunRow.tenant_id == tenant_id,
                    WorkflowRunRow.workflow_id == definition.workflow_id,
                    WorkflowRunRow.workflow_version == definition.version,
                    WorkflowRunRow.client_idempotency_key == idempotency_key,
                )
            )
            # 2. 指纹或主体不一致说明同键不同请求，属于幂等冲突。
            if existing is not None:
                if (
                    existing.subject_id != subject_id
                    or existing.request_fingerprint != request_fingerprint
                ):
                    raise WorkflowIdempotencyConflict(
                        "workflow idempotency key was already used for another request"
                    )
                # 3. 命中且归属一致时复用既有运行。
                return _run(existing), True
            # 4. 未命中则创建新运行：分配 run_id 与独占 thread_id，状态为 ACCEPTED。
            now = datetime.now(UTC)
            row = WorkflowRunRow(
                run_id=f"run-{uuid4().hex}",
                tenant_id=tenant_id,
                subject_id=subject_id,
                workflow_id=definition.workflow_id,
                workflow_version=definition.version,
                assistant_id=definition.assistant_id,
                deployment_revision=definition.deployment_revision,
                model_profile_id=definition.model_profile_id,
                run_timeout_seconds=definition.timeout_policy.run_timeout_seconds,
                approval_timeout_seconds=definition.timeout_policy.approval_timeout_seconds,
                thread_id=str(uuid4()),
                client_idempotency_key=idempotency_key,
                arguments_hash=arguments_hash,
                request_fingerprint=request_fingerprint,
                input_payload=input_payload,
                artifact_refs=[],
                status=WorkflowRunStatus.ACCEPTED.value,
                started_at=now,
                updated_at=now,
            )
            session.add(row)
        return _run(row), False

    def bind_server_run(self, run_id: str, server_run_id: str, status: str) -> WorkflowRun:
        """把运行绑定到 Agent Server 的 server run 并归一化同步状态。

        Raises:
            WorkflowNotFound: 运行不存在。
            WorkflowConflict: 已绑定到其他 server run。

        """
        with self._sessions.begin() as session:
            row = session.get(WorkflowRunRow, run_id)
            if row is None:
                raise WorkflowNotFound("workflow run was not found")
            # 一次运行至多绑定一个 server run；重复绑定同一值视为幂等。
            if row.server_run_id is not None and row.server_run_id != server_run_id:
                raise WorkflowConflict("workflow run is already bound to another server run")
            row.server_run_id = server_run_id
            row.status = _normalize_status(status).value
            row.updated_at = datetime.now(UTC)
        return _run(row)

    def get_owned(self, run_id: str, tenant_id: str, subject_id: str) -> WorkflowRun:
        """读取属于指定租户与主体的运行记录。

        Raises:
            WorkflowNotFound: 运行不存在或不属于该主体。

        """
        statement = select(WorkflowRunRow).where(
            WorkflowRunRow.run_id == run_id,
            WorkflowRunRow.tenant_id == tenant_id,
            WorkflowRunRow.subject_id == subject_id,
        )
        with self._sessions() as session:
            row = session.scalar(statement)
            if row is None:
                raise WorkflowNotFound("workflow run was not found for authenticated owner")
            return _run(row)

    def set_status(
        self,
        run_id: str,
        status: WorkflowRunStatus,
        *,
        output_payload: dict[str, Any] | None = None,
        artifact_refs: tuple[str, ...] = (),
    ) -> tuple[WorkflowRun, bool]:
        """更新运行状态，可选写入输出与制品引用。

        Returns:
            二元组：（更新后的运行，状态是否发生变化）。

        Raises:
            WorkflowNotFound: 运行不存在。
            WorkflowConflict: 终态被改写为其他状态。

        """
        with self._sessions.begin() as session:
            row = session.get(WorkflowRunRow, run_id)
            if row is None:
                raise WorkflowNotFound("workflow run was not found")
            # 1. 终态保护：终态不可改为其他状态，同状态重放视为幂等。
            terminal = {
                WorkflowRunStatus.COMPLETED.value,
                WorkflowRunStatus.REJECTED.value,
                WorkflowRunStatus.FAILED.value,
            }
            if row.status in terminal and row.status != status.value:
                raise WorkflowConflict("terminal workflow status cannot be changed")
            # 2. 记录状态变化，并按需写入输出与去重后的制品引用。
            changed = row.status != status.value
            row.status = status.value
            row.updated_at = datetime.now(UTC)
            if output_payload is not None:
                row.output_payload = output_payload
            if artifact_refs:
                row.artifact_refs = list(dict.fromkeys(artifact_refs))
            # 3. 进入终态时补记完成时间（仅在首次进入时生效）。
            if status in {
                WorkflowRunStatus.COMPLETED,
                WorkflowRunStatus.REJECTED,
                WorkflowRunStatus.FAILED,
            }:
                row.completed_at = row.completed_at or datetime.now(UTC)
        return _run(row), changed

    def list_incomplete(self) -> tuple[WorkflowRun, ...]:
        """列出全部未进入终态的运行，按启动时间升序。"""
        terminal = (
            WorkflowRunStatus.COMPLETED.value,
            WorkflowRunStatus.REJECTED.value,
            WorkflowRunStatus.FAILED.value,
        )
        statement = (
            select(WorkflowRunRow)
            .where(WorkflowRunRow.status.not_in(terminal))
            .order_by(WorkflowRunRow.started_at)
        )
        with self._sessions() as session:
            return tuple(_run(row) for row in session.scalars(statement))

    def ensure_approval(self, approval: WorkflowApproval) -> tuple[WorkflowApproval, bool]:
        """登记审批请求；同一运行的同一检查点幂等复用。

        Returns:
            二元组：（审批记录，是否为新创建）。

        Raises:
            WorkflowConflict: 既有审批的标识或参数哈希与本次不一致。

        """
        with self._sessions.begin() as session:
            # 1. 幂等查询：同一运行的同一审批点只允许一条审批。
            existing = session.scalar(
                select(WorkflowApprovalRow).where(
                    WorkflowApprovalRow.run_id == approval.run_id,
                    WorkflowApprovalRow.approval_point == approval.approval_point,
                )
            )
            # 2. 既有审批必须与本次标识及参数哈希一致，防止检查点漂移。
            if existing is not None:
                if (
                    existing.approval_id != approval.approval_id
                    or existing.arguments_hash != approval.arguments_hash
                ):
                    raise WorkflowConflict("workflow approval checkpoint changed unexpectedly")
                return _approval(existing), False
            # 3. 不存在时落库新的审批请求。
            row = WorkflowApprovalRow(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
                subject_id=approval.subject_id,
                approval_point=approval.approval_point,
                arguments_hash=approval.arguments_hash,
                requested_action=approval.requested_action,
                request_payload=approval.request_payload,
                allowed_decisions=list(approval.allowed_decisions),
                required_scope=approval.required_scope,
                status=approval.status.value,
                requested_at=approval.requested_at,
                expires_at=approval.expires_at,
            )
            session.add(row)
        return _approval(row), True

    def get_approval(self, run_id: str) -> WorkflowApproval:
        """读取指定运行最近一次（按请求时间）审批请求。

        Raises:
            WorkflowNotFound: 不存在审批记录。

        """
        with self._sessions() as session:
            row = session.scalar(
                select(WorkflowApprovalRow)
                .where(WorkflowApprovalRow.run_id == run_id)
                .order_by(WorkflowApprovalRow.requested_at.desc())
            )
            if row is None:
                raise WorkflowNotFound("workflow approval was not found")
            return _approval(row)

    def decide_approval(
        self,
        approval_id: str,
        *,
        status: WorkflowApprovalStatus,
        decided_by: str,
        reason: str | None,
        decided_at: datetime,
    ) -> tuple[WorkflowApproval, bool]:
        """落成审批决定；重复同一决定时幂等返回。

        Returns:
            二元组：（审批记录，是否为本次新决定）。

        Raises:
            WorkflowNotFound: 审批不存在。
            WorkflowConflict: 审批已被以不同内容决定过。

        """
        with self._sessions.begin() as session:
            row = session.get(WorkflowApprovalRow, approval_id)
            if row is None:
                raise WorkflowNotFound("workflow approval was not found")
            # 1. 非待定状态：同一决定幂等重放，其他情况视为重复决定冲突。
            if row.status != WorkflowApprovalStatus.PENDING.value:
                if row.status == status.value and row.decided_by == decided_by:
                    return _approval(row), False
                raise WorkflowConflict("workflow approval has already been decided")
            # 2. 待定状态：落成决定状态、决定人、理由与决定时间。
            row.status = status.value
            row.decided_by = decided_by
            row.decision_reason = reason
            row.decided_at = decided_at
        return _approval(row), True


def _normalize_status(status: str) -> WorkflowRunStatus:
    """把 Agent Server 侧状态字符串归一化为运行状态枚举，未知值退化为 PENDING。"""
    return {
        "accepted": WorkflowRunStatus.ACCEPTED,
        "pending": WorkflowRunStatus.PENDING,
        "running": WorkflowRunStatus.RUNNING,
        "interrupted": WorkflowRunStatus.INTERRUPTED,
        "success": WorkflowRunStatus.COMPLETED,
        "completed": WorkflowRunStatus.COMPLETED,
        "error": WorkflowRunStatus.FAILED,
        "failed": WorkflowRunStatus.FAILED,
    }.get(status, WorkflowRunStatus.PENDING)
