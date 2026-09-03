"""定义该版本数据库结构变更及其可逆迁移步骤。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_delegations"
down_revision: str | None = "0003_stage4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建本版本新增的表、索引和约束。"""
    op.create_table(
        "delegations",
        sa.Column("delegation_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(128),
            sa.ForeignKey("conversations.conversation_id"),
            nullable=False,
        ),
        sa.Column(
            "parent_turn_id",
            sa.String(128),
            sa.ForeignKey("conversation_turns.turn_id"),
            nullable=False,
        ),
        sa.Column("parent_run_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("target_version", sa.String(32), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("authorization_decision", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("child_run_id", sa.String(128)),
        sa.Column("child_thread_id", sa.String(128)),
        sa.Column("child_server_run_id", sa.String(128)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("output_payload", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("child_run_id", name="uq_delegations_child_run"),
    )
    op.create_index(
        "ix_delegations_parent",
        "delegations",
        ["tenant_id", "subject_id", "parent_run_id", "created_at"],
    )
    op.create_index(
        "ix_delegations_incomplete",
        "delegations",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    """按依赖逆序移除本版本引入的数据库对象。"""
    op.drop_index("ix_delegations_incomplete", table_name="delegations")
    op.drop_index("ix_delegations_parent", table_name="delegations")
    op.drop_table("delegations")
