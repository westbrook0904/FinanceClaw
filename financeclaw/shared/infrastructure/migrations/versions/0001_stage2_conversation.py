"""首个迁移（stage2）：创建会话域的基础表。

建立多租户会话模型的核心表结构：``conversations``（会话）、
``conversation_turns``（轮次，含幂等键约束）、``conversation_messages``
（消息，按会话内序号唯一）、``conversation_summaries``（分层摘要）、
``model_context_manifests``（模型上下文清单，可审计复现上下文组装）
以及 ``artifacts``（产物登记），并附各查询路径所需索引。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 本迁移的版本标识，作为迁移链的起点。
revision: str = "0001_stage2"
# 前驱版本：无（首个迁移）。
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 stage2 会话域的全部表、约束与索引。"""
    # 1. 会话主表：租户+主体定位归属，agent_thread_id 唯一映射服务端线程。
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("agent_profile_version", sa.String(32), nullable=False),
        sa.Column("agent_thread_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("agent_thread_id", name="uq_conversations_agent_thread"),
    )
    op.create_index(
        "ix_conversations_owner",
        "conversations",
        ["tenant_id", "subject_id", "conversation_id"],
    )
    # 2. 轮次表：run_id 唯一 + 客户端幂等键唯一，支撑重试与去重。
    op.create_table(
        "conversation_turns",
        sa.Column("turn_id", sa.String(128), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(128),
            sa.ForeignKey("conversations.conversation_id"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("server_run_id", sa.String(128)),
        sa.Column("client_idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("target_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", name="uq_conversation_turns_run"),
        sa.UniqueConstraint(
            "tenant_id",
            "subject_id",
            "client_idempotency_key",
            name="uq_conversation_turns_owner_idempotency",
        ),
    )
    op.create_index(
        "ix_conversation_turns_conversation_created",
        "conversation_turns",
        ["conversation_id", "created_at"],
    )
    # 3. 消息表：会话内 sequence 唯一，支持 parent_message_id 组织树状结构。
    op.create_table(
        "conversation_messages",
        sa.Column("message_id", sa.String(128), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(128),
            sa.ForeignKey("conversations.conversation_id"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            sa.String(128),
            sa.ForeignKey("conversation_turns.turn_id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "parent_message_id",
            sa.String(128),
            sa.ForeignKey("conversation_messages.message_id"),
        ),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_messages_conversation_sequence"
        ),
    )
    op.create_index("ix_messages_turn_role", "conversation_messages", ["turn_id", "role"])
    # 4. 分层摘要表：按 level 与消息序号区间组织，superseded_by 记录被替换关系。
    op.create_table(
        "conversation_summaries",
        sa.Column("summary_id", sa.String(128), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(128),
            sa.ForeignKey("conversations.conversation_id"),
            nullable=False,
        ),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("start_sequence", sa.Integer(), nullable=False),
        sa.Column("end_sequence", sa.Integer(), nullable=False),
        sa.Column("source_message_ids", sa.JSON(), nullable=False),
        sa.Column("source_summary_ids", sa.JSON(), nullable=False),
        sa.Column("summary_content", sa.Text(), nullable=False),
        sa.Column("topics", sa.JSON(), nullable=False),
        sa.Column("entities", sa.JSON(), nullable=False),
        sa.Column("decisions", sa.JSON(), nullable=False),
        sa.Column("open_items", sa.JSON(), nullable=False),
        sa.Column("model_profile_version", sa.String(32), nullable=False),
        sa.Column("template_version", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "superseded_by",
            sa.String(128),
            sa.ForeignKey("conversation_summaries.summary_id"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_summaries_conversation_range",
        "conversation_summaries",
        ["conversation_id", "level", "start_sequence", "end_sequence"],
    )
    # 5. 模型上下文清单表：记录每次模型调用的上下文组装来源，model_call_id 唯一。
    op.create_table(
        "model_context_manifests",
        sa.Column("manifest_id", sa.String(128), primary_key=True),
        sa.Column("model_call_id", sa.String(128), nullable=False),
        sa.Column(
            "conversation_id",
            sa.String(128),
            sa.ForeignKey("conversations.conversation_id"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            sa.String(128),
            sa.ForeignKey("conversation_turns.turn_id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("prompt_template_version", sa.String(64), nullable=False),
        sa.Column("agent_profile_version", sa.String(32), nullable=False),
        sa.Column("model_profile_version", sa.String(32), nullable=False),
        sa.Column("recent_message_start", sa.Integer()),
        sa.Column("recent_message_end", sa.Integer()),
        sa.Column("summary_ids", sa.JSON(), nullable=False),
        sa.Column("memory_ids", sa.JSON(), nullable=False),
        sa.Column("historical_message_ids", sa.JSON(), nullable=False),
        sa.Column("tool_result_refs", sa.JSON(), nullable=False),
        sa.Column("exposed_tools", sa.JSON(), nullable=False),
        sa.Column("input_token_count", sa.Integer(), nullable=False),
        sa.Column("available_input_tokens", sa.Integer(), nullable=False),
        sa.Column("omissions", sa.JSON(), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("model_call_id", name="uq_manifests_model_call"),
    )
    op.create_index(
        "ix_manifests_run",
        "model_context_manifests",
        ["conversation_id", "turn_id", "run_id"],
    )
    # 6. 产物登记表：记录产物存储位置、内容哈希与访问/加密元数据。
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("content_type", sa.String(200), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("access_policy", sa.JSON(), nullable=False),
        sa.Column("encryption_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artifacts_owner", "artifacts", ["tenant_id", "subject_id", "artifact_id"])


def downgrade() -> None:
    """回滚 stage2：按依赖逆序删除全部索引与表。"""
    op.drop_index("ix_artifacts_owner", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_index("ix_manifests_run", table_name="model_context_manifests")
    op.drop_table("model_context_manifests")
    op.drop_index("ix_summaries_conversation_range", table_name="conversation_summaries")
    op.drop_table("conversation_summaries")
    op.drop_index("ix_messages_turn_role", table_name="conversation_messages")
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversation_turns_conversation_created", table_name="conversation_turns")
    op.drop_table("conversation_turns")
    op.drop_index("ix_conversations_owner", table_name="conversations")
    op.drop_table("conversations")
