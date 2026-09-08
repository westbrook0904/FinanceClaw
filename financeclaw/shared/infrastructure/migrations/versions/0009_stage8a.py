"""Stage-8A：共享库受理、有限授权与独立 Coordinator 的持久化责任。"""

import sqlalchemy as sa
from alembic import op

revision = "0009_stage8a"
down_revision = "0008_stage6fix_c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """只新增结构，不启动旧任务，不改变原发布或运行输入。"""
    op.create_table(
        "coordinated_runs",
        sa.Column(
            "run_id",
            sa.String(length=128),
            sa.ForeignKey("run_executions.run_id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(length=128),
            sa.ForeignKey("conversations.conversation_id"),
            nullable=False,
        ),
        sa.Column("backend_instance_id", sa.String(length=128), nullable=False),
        sa.Column("driver_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("projection", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("wake", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_coordinated_due",
        "coordinated_runs",
        ["backend_instance_id", "active", "due_at"],
        unique=False,
    )
    op.create_index(
        "uq_coordinated_active_conversation",
        "coordinated_runs",
        ["conversation_id"],
        unique=True,
        sqlite_where=sa.text("active = 1"),
        postgresql_where=sa.text("active = true"),
    )
    op.create_table(
        "run_authorizations",
        sa.Column(
            "run_id",
            sa.String(length=128),
            sa.ForeignKey("run_executions.run_id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "coordination_inbox",
        sa.Column("inbox_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "run_id", sa.String(length=128), sa.ForeignKey("coordinated_runs.run_id"), nullable=True
        ),
        sa.Column("backend_instance_id", sa.String(length=128), nullable=False),
        sa.Column("execution_hash", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_coordination_inbox_binding",
        "coordination_inbox",
        ["backend_instance_id", "execution_hash", "processed"],
        unique=False,
    )
    op.create_index(
        "ix_coordination_inbox_expiry", "coordination_inbox", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_coordination_inbox_root", "coordination_inbox", ["run_id", "processed"], unique=False
    )
    op.create_table(
        "backend_attempts",
        sa.Column(
            "operation_id",
            sa.String(length=128),
            sa.ForeignKey("run_operations.operation_id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(length=128),
            sa.ForeignKey("coordinated_runs.run_id"),
            nullable=False,
        ),
        sa.Column("backend_instance_id", sa.String(length=128), nullable=False),
        sa.Column("execution_hash", sa.String(length=64), nullable=False),
        sa.Column("reference", sa.JSON(), nullable=False),
        sa.Column("cancellation_confirmed", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_backend_attempts_run_id", "backend_attempts", ["run_id"], unique=False)
    op.create_index(
        "uq_backend_attempt_identity",
        "backend_attempts",
        ["backend_instance_id", "execution_hash"],
        unique=True,
    )
    op.create_table(
        "coordination_continuations",
        sa.Column("continuation_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=128),
            sa.ForeignKey("coordinated_runs.run_id"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(length=128), nullable=False, unique=True),
        sa.Column("reference", sa.JSON(), nullable=False),
        sa.Column("binding", sa.JSON(), nullable=False),
        sa.Column("applied_operation_id", sa.String(length=128), nullable=True),
        sa.Column("application_evidence", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_coordination_continuations_run_id",
        "coordination_continuations",
        ["run_id"],
        unique=False,
    )
    op.create_table(
        "run_progress_events",
        sa.Column(
            "run_id",
            sa.String(length=128),
            sa.ForeignKey("coordinated_runs.run_id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "coordinator_heartbeats",
        sa.Column("worker_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column("backend_instance_id", sa.String(length=128), nullable=False),
        sa.Column("driver_version", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_coordinator_heartbeats_backend_instance_id",
        "coordinator_heartbeats",
        ["backend_instance_id"],
        unique=False,
    )


def downgrade() -> None:
    """存在协调事实时拒绝破坏性回滚，须先显式制定归档迁移。"""
    for table in (
        "coordinated_runs",
        "run_authorizations",
        "coordination_inbox",
        "backend_attempts",
        "coordination_continuations",
        "run_progress_events",
        "coordinator_heartbeats",
    ):
        if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM " + table)):
            raise RuntimeError(
                "coordinated runs require an explicit archival migration before downgrade"
            )
    op.drop_table("coordinator_heartbeats")
    op.drop_table("run_progress_events")
    op.drop_table("coordination_continuations")
    op.drop_table("backend_attempts")
    op.drop_table("coordination_inbox")
    op.drop_table("run_authorizations")
    op.drop_table("coordinated_runs")
