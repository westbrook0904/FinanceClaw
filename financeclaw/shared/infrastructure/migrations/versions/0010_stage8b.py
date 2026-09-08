"""Stage-8B durable notification facts and fenced delivery receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0010_stage8b"
down_revision = "0009_stage8a"
branch_labels = None
depends_on = None


def upgrade():
    """仅新增通知表，不补发历史事件。"""
    op.create_table(
        "notification_targets",
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("binding_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("app_id", sa.String(length=128), nullable=False),
        sa.Column("address", sa.JSON(), nullable=False),
        sa.Column("delivery_mode", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["channel_conversation_bindings.binding_id"],
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["run_executions.run_id"],
        ),
        sa.PrimaryKeyConstraint("target_id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index(
        op.f("ix_notification_targets_app_id"), "notification_targets", ["app_id"], unique=False
    )
    op.create_table(
        "notification_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["notification_targets.target_id"],
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_notification_events_pending",
        "notification_events",
        ["materialized_at", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_events_target_id"), "notification_events", ["target_id"], unique=False
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("delivery_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("part", sa.Integer(), nullable=False),
        sa.Column("parts", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("content_version", sa.Integer(), nullable=False),
        sa.Column("send_key", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("uncertain", sa.Boolean(), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recover_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_evidence_hash", sa.String(length=64), nullable=True),
        sa.Column("message_id", sa.String(length=128), nullable=True),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["notification_events.event_id"],
        ),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint("send_key"),
    )
    op.create_index(
        "ix_notification_deliveries_due",
        "notification_deliveries",
        ["due_at", "lease_until"],
        unique=False,
    )
    op.create_index(
        "uq_notification_delivery_part",
        "notification_deliveries",
        ["event_id", "part"],
        unique=True,
    )
    op.create_table(
        "notification_senders",
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("app_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        op.f("ix_notification_senders_app_id"), "notification_senders", ["app_id"], unique=False
    )
    # ### end Alembic commands ###


def downgrade():
    """有通知事实时拒绝破坏性回滚；先停受理，保留兼容发送器。"""
    tables = (
        "notification_deliveries",
        "notification_events",
        "notification_targets",
        "notification_senders",
    )
    connection = op.get_bind()
    for table in tables:
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Stage-8B notification evidence must be retained")
    for table in tables:
        op.drop_table(table)
