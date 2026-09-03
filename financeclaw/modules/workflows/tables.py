"""声明工作流运行与审批的 SQLAlchemy 表映射。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from financeclaw.infrastructure.orm import Base, utcnow


class WorkflowRunRow(Base):
    """定义工作流运行Row。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        __tablename__: 内部 `tablename  ` 状态或依赖，不属于公开接口。
        __table_args__: 内部 `table args  ` 状态或依赖，不属于公开接口。
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        workflow_id: 工作流的稳定标识。
        workflow_version: 本次运行固定使用的工作流版本。
        assistant_id: 提交 Agent Server 时使用的助手或图标识。
        deployment_revision: 构建工作流图的部署修订号，用于定位实际运行代码。
        model_profile_id: 关联对象的稳定标识，用于查询、关联和审计追踪。
        run_timeout_seconds: 该操作允许的最长时间（秒）。
        approval_timeout_seconds: 该操作允许的最长时间（秒）。
        thread_id: Agent Server 线程标识，用于保存运行检查点与消息状态。
        server_run_id: Agent Server 侧运行标识；尚未提交远端运行时为空。
        client_idempotency_key: 客户端幂等键，在同一资源范围内唯一。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定和篡改检测。
        request_fingerprint: 委派或工作流请求的稳定指纹，用于幂等冲突检测。
        input_payload: 提交给工作流的规范化输入快照。
        output_payload: 运行终态时保存的结构化输出快照。
        artifact_refs: 本次运行、审计或事件关联的制品标识集合。
        status: 当前生命周期状态，决定记录允许的后续操作。
        started_at: 该生命周期事件发生的 UTC 时间。
        updated_at: 最近一次状态或内容变更时间，统一按 UTC 解释。
        completed_at: 进入成功或失败终态的时间；未结束时为空。
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
    """定义工作流审批Row。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        __tablename__: 内部 `tablename  ` 状态或依赖，不属于公开接口。
        __table_args__: 内部 `table args  ` 状态或依赖，不属于公开接口。
        approval_id: 审批请求稳定标识。
        run_id: 应用侧运行标识，用于跨服务查询、追踪和幂等关联。
        tenant_id: 租户隔离键，所有读取和写入都必须以此限定边界。
        subject_id: 已认证主体标识，用于所有权校验和审计归因。
        approval_point: 工作流中触发本次人工确认的稳定节点名称。
        arguments_hash: 规范化参数的 SHA-256，用于审批绑定和篡改检测。
        requested_action: 需要人工确认的具体操作。
        request_payload: 审批点展示并绑定哈希的请求参数快照。
        allowed_decisions: 当前配置明确允许的值集合。
        required_scope: 作出该审批决定所需的权限域。
        status: 当前生命周期状态，决定记录允许的后续操作。
        requested_at: 该生命周期事件发生的 UTC 时间。
        expires_at: 记录或审批失效时间；为空表示不按时间自动失效。
        decided_at: 该生命周期事件发生的 UTC 时间。
        decided_by: 作出审批决定的主体标识。
        decision_reason: 审批人或策略给出的决定理由。
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
