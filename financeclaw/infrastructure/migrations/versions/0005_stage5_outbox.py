"""Add the Stage-5 transactional outbox.

Revision ID: 0005_stage5
Revises: 0004_delegations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_stage5"
down_revision: str | None = "0004_delegations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
    )
    op.create_index(
        "ix_outbox_delivery",
        "outbox_events",
        ["status", "available_at", "locked_until"],
    )
    op.create_index(
        "ix_outbox_owner",
        "outbox_events",
        ["tenant_id", "subject_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_owner", table_name="outbox_events")
    op.drop_index("ix_outbox_delivery", table_name="outbox_events")
    op.drop_table("outbox_events")
