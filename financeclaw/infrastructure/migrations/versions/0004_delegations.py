"""委派迁移：创建跨会话委派执行记录表。

``delegations`` 记录父会话轮次向子运行（嵌套会话/工作流）委派任务的
完整链路：授权决策、目标与参数指纹、子运行标识、输出与交付时间；
子运行 ID 唯一约束防止重复委派。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 本迁移的版本标识。
revision: str = "0004_delegations"
# 前驱版本：0003_stage4（工作流运行与审批表）。
down_revision: str | None = "0003_stage4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建委派记录表及其索引。"""
    # 1. 委派表：外键关联父会话与父轮次，child_run_id 唯一防重复委派。
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
    # 2. 父链路索引：按租户+主体+父运行回溯其全部委派及其时间线。
    op.create_index(
        "ix_delegations_parent",
        "delegations",
        ["tenant_id", "subject_id", "parent_run_id", "created_at"],
    )
    # 3. 未完成扫描索引：供后台扫描超时与卡住的委派。
    op.create_index(
        "ix_delegations_incomplete",
        "delegations",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    """回滚委派迁移：删除其两个索引与委派表。"""
    op.drop_index("ix_delegations_incomplete", table_name="delegations")
    op.drop_index("ix_delegations_parent", table_name="delegations")
    op.drop_table("delegations")
