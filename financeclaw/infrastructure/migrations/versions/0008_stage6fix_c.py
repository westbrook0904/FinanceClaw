"""Stage 6 Fix C：单位置的持久化用户交互与精确恢复。"""

import sqlalchemy as sa
from alembic import op

revision = "0008_stage6fix_c"
down_revision = "0007_stage6fix_ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增量建表，不把旧中断猜测为已授权的可恢复交互。"""
    op.create_table(
        "pending_interactions",
        sa.Column("interaction_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("conversation_id", sa.String(128)),
        sa.Column("root_run_id", sa.String(128), nullable=False),
        sa.Column(
            "owner_run_id", sa.String(128), sa.ForeignKey("run_executions.run_id"), nullable=False
        ),
        sa.Column("parent_run_id", sa.String(128)),
        sa.Column("delegation_id", sa.String(128)),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column("server_run_id", sa.String(128), nullable=False),
        sa.Column("interrupt_id", sa.String(128), nullable=False),
        sa.Column("checkpoint_id", sa.String(128)),
        sa.Column("point_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by", sa.String(128)),
        sa.Column("response_key", sa.String(256)),
        sa.Column("response_hash", sa.String(64)),
        sa.Column("response", sa.JSON()),
        sa.Column("operation_id", sa.String(128)),
    )
    op.create_index("ix_pending_interactions_root_run_id", "pending_interactions", ["root_run_id"])
    op.create_index(
        "ix_pending_interactions_owner_run_id", "pending_interactions", ["owner_run_id"]
    )
    op.create_index(
        "uq_interactions_pending_root",
        "pending_interactions",
        ["root_run_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    """只允许尚未产生交互事实的空升级回滚，不删除用户决定及其恢复证据。"""
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM pending_interactions")):
        raise RuntimeError(
            "stage6fix interactions require an explicit archival migration before downgrade"
        )
    op.drop_table("pending_interactions")
