"""stage6 迁移：创建飞书单聊到 Conversation 的稳定绑定表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 本迁移的版本标识。
revision: str = "0006_stage6"
# 前驱版本：stage5 Outbox。
down_revision: str | None = "0005_stage5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 Channel Conversation 绑定表、唯一约束与用户检索索引。"""
    op.create_table(
        "channel_conversation_bindings",
        sa.Column("binding_id", sa.String(128), primary_key=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("app_id", sa.String(128), nullable=False),
        sa.Column("tenant_key", sa.String(128), nullable=False),
        sa.Column("external_user_id", sa.String(128), nullable=False),
        sa.Column("external_chat_id", sa.String(128), nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(128),
            sa.ForeignKey("conversations.conversation_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "channel",
            "app_id",
            "tenant_key",
            "external_chat_id",
            name="uq_channel_conversation_bindings_chat",
        ),
    )
    op.create_index(
        "ix_channel_conversation_bindings_user",
        "channel_conversation_bindings",
        ["channel", "app_id", "tenant_key", "external_user_id"],
    )


def downgrade() -> None:
    """回滚 stage6：删除用户索引与 Channel Conversation 绑定表。"""
    op.drop_index(
        "ix_channel_conversation_bindings_user",
        table_name="channel_conversation_bindings",
    )
    op.drop_table("channel_conversation_bindings")
