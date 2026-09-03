"""Persist Stage-4 published workflow runs and approval decisions.

Revision ID: 0003_stage4
Revises: 0002_stage3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_stage4"
down_revision: str | None = "0002_stage3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("workflow_id", sa.String(128), nullable=False),
        sa.Column("workflow_version", sa.String(32), nullable=False),
        sa.Column("assistant_id", sa.String(128), nullable=False),
        sa.Column("deployment_revision", sa.String(128), nullable=False),
        sa.Column("model_profile_id", sa.String(128), nullable=False),
        sa.Column("run_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("approval_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column("server_run_id", sa.String(128)),
        sa.Column("client_idempotency_key", sa.String(200), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_payload", sa.JSON()),
        sa.Column("artifact_refs", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("thread_id", name="uq_workflow_runs_thread"),
        sa.UniqueConstraint("server_run_id", name="uq_workflow_runs_server_run"),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "workflow_version",
            "client_idempotency_key",
            name="uq_workflow_runs_release_idempotency",
        ),
    )
    op.create_index(
        "ix_workflow_runs_owner",
        "workflow_runs",
        ["tenant_id", "subject_id", "run_id"],
    )
    op.create_index(
        "ix_workflow_runs_incomplete",
        "workflow_runs",
        ["status", "updated_at"],
    )
    op.create_table(
        "workflow_approvals",
        sa.Column("approval_id", sa.String(128), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(128),
            sa.ForeignKey("workflow_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("approval_point", sa.String(128), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("requested_action", sa.String(128), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("allowed_decisions", sa.JSON(), nullable=False),
        sa.Column("required_scope", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decided_by", sa.String(128)),
        sa.Column("decision_reason", sa.Text()),
        sa.UniqueConstraint("run_id", "approval_point", name="uq_workflow_approval_point"),
    )
    op.create_index(
        "ix_workflow_approvals_owner",
        "workflow_approvals",
        ["tenant_id", "subject_id", "approval_id"],
    )
    op.create_index(
        "ix_workflow_approvals_pending",
        "workflow_approvals",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_approvals_pending", table_name="workflow_approvals")
    op.drop_index("ix_workflow_approvals_owner", table_name="workflow_approvals")
    op.drop_table("workflow_approvals")
    op.drop_index("ix_workflow_runs_incomplete", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_owner", table_name="workflow_runs")
    op.drop_table("workflow_runs")
