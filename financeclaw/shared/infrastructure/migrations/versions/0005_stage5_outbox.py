"""stage5 迁移：创建 Outbox 事件表，支撑事务性事件发布。

``outbox_events`` 以同库事务先落事件再异步派发（Transactional Outbox
模式）：记录事件载荷、派发状态、重试次数、可用时间与锁租期，由
后台发布器按批次认领并发送。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 本迁移的版本标识。
revision: str = "0005_stage5"
# 前驱版本：0004_delegations（委派记录表）。
down_revision: str | None = "0004_delegations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 Outbox 事件表及其索引。"""
    # 1. Outbox 事件表：payload 记录事件内容，attempts/last_error 支撑重试诊断。
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
    # 2. 派发扫描索引：发布器按状态+可用时间+锁租期认领到期事件。
    op.create_index(
        "ix_outbox_delivery",
        "outbox_events",
        ["status", "available_at", "locked_until"],
    )
    # 3. 归属人索引：按租户+主体回溯事件创建历史。
    op.create_index(
        "ix_outbox_owner",
        "outbox_events",
        ["tenant_id", "subject_id", "created_at"],
    )


def downgrade() -> None:
    """回滚 stage5：删除 Outbox 表的两个索引与事件表。"""
    op.drop_index("ix_outbox_owner", table_name="outbox_events")
    op.drop_index("ix_outbox_delivery", table_name="outbox_events")
    op.drop_table("outbox_events")
