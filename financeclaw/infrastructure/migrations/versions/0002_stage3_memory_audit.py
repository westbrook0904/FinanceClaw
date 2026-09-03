"""Persist Stage-3 Audit records and versioned Manifest memory references.

Revision ID: 0002_stage3
Revises: 0001_stage2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_stage3"
down_revision: str | None = "0001_stage2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The default backfills Stage-2 manifests so an in-place upgrade does not
    # manufacture memory references for model calls that had none.
    op.add_column(
        "model_context_manifests",
        sa.Column(
            "memory_refs",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.create_table(
        "audit_records",
        sa.Column("audit_id", sa.String(128), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("conversation_id", sa.String(128)),
        sa.Column("turn_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("tool_call_id", sa.String(128)),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column("resource_version", sa.String(32), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("artifact_refs", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_audit_owner_time",
        "audit_records",
        ["tenant_id", "subject_id", "occurred_at"],
    )
    op.create_index("ix_audit_run", "audit_records", ["run_id", "event_type"])


def downgrade() -> None:
    op.drop_index("ix_audit_run", table_name="audit_records")
    op.drop_index("ix_audit_owner_time", table_name="audit_records")
    op.drop_table("audit_records")
    op.drop_column("model_context_manifests", "memory_refs")
