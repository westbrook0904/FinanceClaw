"""stage3 迁移：新增记忆引用字段并创建审计记录表。

为 ``model_context_manifests`` 补充 ``memory_refs`` 列（记录上下文中
引用的记忆条目）；创建 ``audit_records`` 表持久化安全审计事件（授权
决策、资源访问等），并提供按归属人与按 run 检索的索引。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 本迁移的版本标识。
revision: str = "0002_stage3"
# 前驱版本：0001_stage2（会话域基础表）。
down_revision: str | None = "0001_stage2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为上下文清单补充记忆引用列，并创建审计记录表及其索引。"""
    # 1. 上下文清单补充 memory_refs：记录本次调用引用的记忆条目 ID。
    op.add_column(
        "model_context_manifests",
        sa.Column(
            "memory_refs",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    # 2. 审计记录表：只增不改的事件流水，记录决策、资源与证据引用。
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
    # 归属人+时间索引：支撑按租户/主体回溯事件的时间线查询。
    op.create_index(
        "ix_audit_owner_time",
        "audit_records",
        ["tenant_id", "subject_id", "occurred_at"],
    )
    # run+事件类型索引：支撑按一次运行聚合并审计其全部动作。
    op.create_index("ix_audit_run", "audit_records", ["run_id", "event_type"])


def downgrade() -> None:
    """回滚 stage3：删除审计表与其索引，并移除 memory_refs 列。"""
    op.drop_index("ix_audit_run", table_name="audit_records")
    op.drop_index("ix_audit_owner_time", table_name="audit_records")
    op.drop_table("audit_records")
    op.drop_column("model_context_manifests", "memory_refs")
