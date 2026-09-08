"""Stage 6 Fix A/B：执行快照、操作对账、独立执行终态和重复审批点实例。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_stage6fix_ab"
down_revision: str | None = "0006_stage6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增量保留历史；原授权缺失的旧运行不自动获得新的执行快照。"""
    op.create_table(
        "run_executions",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("root_run_id", sa.String(128), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("server_run_id", sa.String(128)),
        sa.Column("waiting", sa.JSON()),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.Column("cancellation_confirmed", sa.Boolean(), nullable=False),
        sa.Column("side_effects_denied", sa.Boolean(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("operation_calls", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_executions_root_run_id", "run_executions", ["root_run_id"])
    op.create_table(
        "run_operations",
        sa.Column("operation_id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("run_executions.run_id"), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("server_run_id", sa.String(128)),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_operations_run_id", "run_operations", ["run_id"])
    op.add_column("delegations", sa.Column("execution_snapshot", sa.JSON()))
    op.add_column(
        "delegations",
        sa.Column("execution_status", sa.String(32), nullable=False, server_default="unknown"),
    )
    # delivered 没有足够信息时保持 unknown，不编造历史成功或原始授权。
    op.execute("UPDATE delegations SET execution_status = status WHERE status <> 'delivered'")
    with op.batch_alter_table("workflow_approvals") as batch:
        batch.drop_constraint("uq_workflow_approval_point", type_="unique")
        batch.create_index("ix_workflow_approval_point", ["run_id", "approval_point"])
    op.create_index(
        "uq_messages_final_assistant",
        "conversation_messages",
        ["turn_id"],
        unique=True,
        sqlite_where=sa.text("role = 'assistant' AND parent_message_id IS NULL"),
        postgresql_where=sa.text("role = 'assistant' AND parent_message_id IS NULL"),
    )


def downgrade() -> None:
    """尚未受理新任务时可回滚；已有执行事实必须先做专门的归档迁移。"""
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT COUNT(*) FROM run_executions")) or bind.scalar(
        sa.text("SELECT COUNT(*) FROM delegations WHERE execution_snapshot IS NOT NULL")
    ):
        raise RuntimeError(
            "stage6fix execution facts require an explicit archival migration before downgrade"
        )
    op.drop_index("uq_messages_final_assistant", table_name="conversation_messages")
    with op.batch_alter_table("workflow_approvals") as batch:
        batch.drop_index("ix_workflow_approval_point")
        batch.create_unique_constraint("uq_workflow_approval_point", ["run_id", "approval_point"])
    with op.batch_alter_table("delegations") as batch:
        batch.drop_column("execution_status")
        batch.drop_column("execution_snapshot")
    op.drop_table("run_operations")
    op.drop_table("run_executions")
