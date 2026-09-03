"""工作流运行与审批的 SQLAlchemy ORM 表定义。

workflow_runs 保存运行事实与 thread/server run 映射，workflow_approvals
保存审批决定，两者以级联外键关联，供 BFF 永久保存与追溯。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.infrastructure.orm import Base, utcnow


class WorkflowRunRow(Base):
    """workflow_runs 表的 ORM 映射，持久化一次工作流运行的全部事实。

    使用场景：
        由 SqlAlchemyWorkflowRepository 读写；thread_id 与 server_run_id
        全表唯一保证 Workflow 独占 thread 且一运行至多一个远端执行，
        （租户，流程，版本，幂等键）唯一保证发布幂等。

    Attributes:
        run_id: 应用侧运行标识，主键。
        tenant_id: 租户隔离键。
        subject_id: 已认证主体标识，用于所有权校验。
        workflow_id: 工作流稳定标识。
        workflow_version: 本次运行固定的工作流版本。
        assistant_id: Agent Server 侧助手标识。
        deployment_revision: 装配该运行所用的部署修订号。
        model_profile_id: 本次运行固定的模型档案标识。
        run_timeout_seconds: 运行超时快照（秒）。
        approval_timeout_seconds: 审批超时快照（秒）。
        thread_id: Workflow 独占的 Agent Server thread 标识，全表唯一。
        server_run_id: 绑定的 Agent Server 运行标识，全表唯一；未绑定时为空。
        client_idempotency_key: 客户端幂等键，参与发布幂等唯一约束。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定与篡改检测。
        request_fingerprint: 请求指纹 SHA-256，用于幂等冲突检测。
        input_payload: 归一化输入参数的 JSON 快照。
        output_payload: 终态输出的 JSON 快照；未结束时为空。
        artifact_refs: 发布制品标识列表的 JSON 存储，默认为空列表。
        status: 运行状态字符串，取值为 WorkflowRunStatus 的值。
        started_at: 运行创建时间（UTC，带时区）。
        updated_at: 最近一次变更时间（UTC，带时区），更新时自动刷新。
        completed_at: 进入终态的时间；未结束时为空。

    """

    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_workflow_runs_thread"),
        UniqueConstraint("server_run_id", name="uq_workflow_runs_server_run"),
        UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "workflow_version",
            "client_idempotency_key",
            name="uq_workflow_runs_release_idempotency",
        ),
        Index("ix_workflow_runs_owner", "tenant_id", "subject_id", "run_id"),
        Index("ix_workflow_runs_incomplete", "status", "updated_at"),
    )

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(32), nullable=False)
    assistant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    deployment_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    model_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_timeout_seconds: Mapped[int] = mapped_column(nullable=False)
    approval_timeout_seconds: Mapped[int] = mapped_column(nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    server_run_id: Mapped[str | None] = mapped_column(String(128))
    client_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    artifact_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowApprovalRow(Base):
    """workflow_approvals 表的 ORM 映射，持久化人工审批事实。

    使用场景：
        由 SqlAlchemyWorkflowRepository 读写；（run_id, approval_point）
        唯一保证同一运行的同一检查点只有一条审批，挂起状态索引支撑过期清理。

    Attributes:
        approval_id: 审批请求稳定标识，主键。
        run_id: 关联的运行标识，外键指向 workflow_runs，级联删除。
        tenant_id: 租户隔离键。
        subject_id: 发起运行的主体标识。
        approval_point: 触发审批的检查点标识。
        arguments_hash: 绑定的输入参数哈希，恢复前用于复验。
        requested_action: 请求人工确认的具体动作。
        request_payload: 展示给审批人的请求参数 JSON 快照。
        allowed_decisions: 允许决定值列表的 JSON 存储。
        required_scope: 作出决定所需的权限域。
        status: 审批状态字符串，取值为 WorkflowApprovalStatus 的值。
        requested_at: 审批请求创建时间（UTC，带时区）。
        expires_at: 审批过期时间（UTC，带时区），超时后不得再决定。
        decided_at: 决定时间；未决定时为空。
        decided_by: 决定人主体标识；未决定时为空。
        decision_reason: 决定理由文本；可为空。

    """

    __tablename__ = "workflow_approvals"
    __table_args__ = (
        UniqueConstraint("run_id", "approval_point", name="uq_workflow_approval_point"),
        Index("ix_workflow_approvals_owner", "tenant_id", "subject_id", "approval_id"),
        Index("ix_workflow_approvals_pending", "status", "expires_at"),
    )

    approval_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    approval_point: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_action: Mapped[str] = mapped_column(String(128), nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    allowed_decisions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    required_scope: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decision_reason: Mapped[str | None] = mapped_column(Text)
