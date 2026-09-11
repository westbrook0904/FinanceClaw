"""Stage 10 initial schema, for an empty application database only."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """Create the 14-table application schema; native persistence belongs to Agent Server."""
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("content_type", sa.String(length=200), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=True),
        sa.Column("source_turn_id", sa.String(length=128), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("access_policy", sa.JSON(), nullable=False),
        sa.Column("encryption_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index(
        "ix_artifacts_owner", "artifacts", ["tenant_id", "subject_id", "artifact_id"], unique=False
    )
    op.create_index(
        "ix_artifacts_turn",
        "artifacts",
        ["tenant_id", "subject_id", "conversation_id", "source_turn_id"],
        unique=False,
    )
    op.create_table(
        "audit_records",
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=True),
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("tool_call_id", sa.String(length=128), nullable=True),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("resource_version", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("artifact_refs", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index(
        "ix_audit_owner_time",
        "audit_records",
        ["tenant_id", "subject_id", "occurred_at"],
        unique=False,
    )
    op.create_index("ix_audit_run", "audit_records", ["turn_id", "event_type"], unique=False)
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("agent_profile_version", sa.String(length=32), nullable=False),
        sa.Column("agent_thread_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("conversation_id"),
        sa.UniqueConstraint("agent_thread_id", name="uq_conversations_agent_thread"),
        sa.UniqueConstraint(
            "conversation_id", "tenant_id", "subject_id", name="uq_conversation_owner"
        ),
    )
    op.create_index(
        "ix_conversations_owner",
        "conversations",
        ["tenant_id", "subject_id", "conversation_id"],
        unique=False,
    )
    op.create_table(
        "notification_senders",
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("app_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        op.f("ix_notification_senders_app_id"), "notification_senders", ["app_id"], unique=False
    )
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("destination", sa.String(length=64), nullable=False),
        sa.Column("claim_epoch", sa.Integer(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_outbox_delivery",
        "outbox_events",
        ["destination", "status", "available_at", "locked_until"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_owner", "outbox_events", ["tenant_id", "subject_id", "created_at"], unique=False
    )
    op.create_table(
        "channel_conversation_bindings",
        sa.Column("binding_id", sa.String(length=128), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("app_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_key", sa.String(length=128), nullable=False),
        sa.Column("external_user_id", sa.String(length=128), nullable=False),
        sa.Column("external_chat_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
        ),
        sa.PrimaryKeyConstraint("binding_id"),
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
        unique=False,
    )
    op.create_table(
        "conversation_turns",
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("user_message_id", sa.String(length=128), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column(
            "release_snapshot",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("release_hash", sa.String(length=64), nullable=False),
        sa.Column("current_command_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("status_reason", sa.String(length=64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "grant_scopes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("grant_source", sa.String(length=32), nullable=False),
        sa.Column("grant_source_hash", sa.String(length=64), nullable=False),
        sa.Column("grant_issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_revision", sa.Integer(), nullable=False),
        sa.Column("grant_revoked", sa.Boolean(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("command_calls", sa.Integer(), nullable=False),
        sa.Column("side_effects_denied", sa.Boolean(), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('accepted','queued','running','waiting','resuming','cancelling','blocked',"
            "'completed','failed','cancelled')",
            name="ck_turn_status",
        ),
        sa.CheckConstraint(
            "model_calls >= 0 AND tool_calls >= 0 AND command_calls >= 0",
            name="ck_turn_budget_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "tenant_id", "subject_id"],
            [
                "conversations.conversation_id",
                "conversations.tenant_id",
                "conversations.subject_id",
            ],
            name="fk_turn_owner",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id", "current_command_id"],
            ["turn_commands.turn_id", "turn_commands.command_id"],
            name="fk_turn_current_command",
            initially="DEFERRED",
            deferrable=True,
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["turn_id", "user_message_id"],
            ["conversation_messages.turn_id", "conversation_messages.message_id"],
            name="fk_turn_user_message",
            initially="DEFERRED",
            deferrable=True,
            use_alter=True,
        ),
        sa.PrimaryKeyConstraint("turn_id"),
        sa.UniqueConstraint(
            "tenant_id", "subject_id", "idempotency_key", name="uq_turn_idempotency"
        ),
        sa.UniqueConstraint("turn_id", "conversation_id", name="uq_turn_conversation"),
    )
    op.create_index(
        "ix_turn_conversation_created",
        "conversation_turns",
        ["conversation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_turn_due",
        "conversation_turns",
        ["next_action_at"],
        unique=False,
        postgresql_where=sa.text("status NOT IN ('completed', 'failed', 'cancelled')"),
        sqlite_where=sa.text("status NOT IN ('completed', 'failed', 'cancelled')"),
    )
    op.create_index(
        "uq_turn_active_conversation",
        "conversation_turns",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('completed', 'failed', 'cancelled')"),
        sqlite_where=sa.text("status NOT IN ('completed', 'failed', 'cancelled')"),
    )
    op.create_table(
        "conversation_messages",
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("parent_message_id", sa.String(length=128), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
        ),
        sa.ForeignKeyConstraint(
            ["parent_message_id"],
            ["conversation_messages.message_id"],
        ),
        sa.ForeignKeyConstraint(
            ["turn_id", "conversation_id"],
            ["conversation_turns.turn_id", "conversation_turns.conversation_id"],
            name="fk_message_turn_conversation",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("message_id"),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_messages_conversation_sequence"
        ),
        sa.UniqueConstraint("turn_id", "message_id", name="uq_message_turn"),
    )
    op.create_index(
        "ix_messages_turn_role", "conversation_messages", ["turn_id", "role"], unique=False
    )
    op.create_index(
        "uq_messages_final_assistant",
        "conversation_messages",
        ["turn_id"],
        unique=True,
        sqlite_where=sa.text("role = 'assistant' AND parent_message_id IS NULL"),
        postgresql_where=sa.text("role = 'assistant' AND parent_message_id IS NULL"),
    )
    op.create_table(
        "model_context_manifests",
        sa.Column("manifest_id", sa.String(length=128), nullable=False),
        sa.Column("model_call_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("prompt_template_version", sa.String(length=64), nullable=False),
        sa.Column("agent_profile_version", sa.String(length=32), nullable=False),
        sa.Column("model_profile_version", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("subtype", sa.String(length=32), nullable=False),
        sa.Column("token_count_method", sa.String(length=64), nullable=False),
        sa.Column("summary_sources", sa.JSON(), nullable=False),
        sa.Column("memory_ids", sa.JSON(), nullable=False),
        sa.Column("memory_refs", sa.JSON(), nullable=False),
        sa.Column("message_ids", sa.JSON(), nullable=False),
        sa.Column("tool_result_refs", sa.JSON(), nullable=False),
        sa.Column("exposed_tools", sa.JSON(), nullable=False),
        sa.Column("input_token_count", sa.Integer(), nullable=False),
        sa.Column("available_input_tokens", sa.Integer(), nullable=False),
        sa.Column("omissions", sa.JSON(), nullable=False),
        sa.Column("context_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["conversation_turns.turn_id"],
        ),
        sa.PrimaryKeyConstraint("manifest_id"),
        sa.UniqueConstraint("model_call_id", name="uq_manifests_model_call"),
    )
    op.create_index(
        "ix_manifests_turn", "model_context_manifests", ["conversation_id", "turn_id"], unique=False
    )
    op.create_table(
        "notification_targets",
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("binding_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("app_id", sa.String(length=128), nullable=False),
        sa.Column("address", sa.JSON(), nullable=False),
        sa.Column("card_id", sa.String(length=128), nullable=True),
        sa.Column("card_message_id", sa.String(length=128), nullable=True),
        sa.Column("card_sequence", sa.Integer(), nullable=False),
        sa.Column("card_payload", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["channel_conversation_bindings.binding_id"],
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["conversation_turns.turn_id"],
        ),
        sa.PrimaryKeyConstraint("target_id"),
        sa.UniqueConstraint("turn_id"),
    )
    op.create_index(
        op.f("ix_notification_targets_app_id"), "notification_targets", ["app_id"], unique=False
    )
    op.create_table(
        "turn_commands",
        sa.Column("command_id", sa.String(length=128), nullable=False),
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "request_payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("grant_revision", sa.Integer(), nullable=False),
        sa.Column(
            "authorized_scopes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("native_run_id", sa.String(length=128), nullable=True),
        sa.Column("receipt_bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "observation_checkpoint",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("observation_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('start','resume')", name="ck_command_kind"),
        sa.CheckConstraint(
            "state IN ('prepared','sending','uncertain','submitted',"
            "'observed','cancelled','rejected')",
            name="ck_command_state",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["conversation_turns.turn_id"], name="fk_command_turn"
        ),
        sa.PrimaryKeyConstraint("command_id"),
        sa.UniqueConstraint("native_run_id"),
        sa.UniqueConstraint("turn_id", "command_id", name="uq_command_turn"),
        sa.UniqueConstraint("turn_id", "sequence", name="uq_command_sequence"),
    )
    op.create_index(op.f("ix_turn_commands_turn_id"), "turn_commands", ["turn_id"], unique=False)
    op.create_index(
        "uq_command_start",
        "turn_commands",
        ["turn_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'start'"),
        sqlite_where=sa.text("kind = 'start'"),
    )
    op.create_table(
        "interactions",
        sa.Column("interaction_id", sa.String(length=128), nullable=False),
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("origin_command_id", sa.String(length=128), nullable=False),
        sa.Column("native_run_id", sa.String(length=128), nullable=False),
        sa.Column(
            "checkpoint",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("interrupt_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "request",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("response_key", sa.String(length=256), nullable=True),
        sa.Column("response_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "response",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("resume_command_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','resolved','rejected','expired','cancelled','superseded')",
            name="ck_interaction_status",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id", "origin_command_id"],
            ["turn_commands.turn_id", "turn_commands.command_id"],
            name="fk_interaction_origin",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id", "resume_command_id"],
            ["turn_commands.turn_id", "turn_commands.command_id"],
            name="fk_interaction_resume",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["conversation_turns.turn_id"], name="fk_interaction_turn"
        ),
        sa.PrimaryKeyConstraint("interaction_id"),
        sa.UniqueConstraint("origin_command_id", "interrupt_id", name="uq_interaction_native"),
        sa.UniqueConstraint("resume_command_id"),
        sa.UniqueConstraint("turn_id", "revision", name="uq_interaction_revision"),
    )
    op.create_index(op.f("ix_interactions_turn_id"), "interactions", ["turn_id"], unique=False)
    op.create_index(
        "uq_interaction_pending",
        "interactions",
        ["turn_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "notification_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["notification_targets.target_id"],
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_notification_events_pending",
        "notification_events",
        ["materialized_at", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_events_target_id"), "notification_events", ["target_id"], unique=False
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("delivery_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("part", sa.Integer(), nullable=False),
        sa.Column("parts", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("message_type", sa.String(length=16), nullable=False),
        sa.Column("card_id", sa.String(length=128), nullable=True),
        sa.Column("target_message_id", sa.String(length=128), nullable=True),
        sa.Column("send_key", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("uncertain", sa.Boolean(), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recover_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_evidence_hash", sa.String(length=64), nullable=True),
        sa.Column("message_id", sa.String(length=128), nullable=True),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["notification_events.event_id"],
        ),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint("send_key"),
    )
    op.create_index(
        "ix_notification_deliveries_due",
        "notification_deliveries",
        ["due_at", "lease_until"],
        unique=False,
    )
    op.create_index(
        "uq_notification_delivery_part",
        "notification_deliveries",
        ["event_id", "part"],
        unique=True,
    )

    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_turn_current_command",
            "conversation_turns",
            "turn_commands",
            ["turn_id", "current_command_id"],
            ["turn_id", "command_id"],
            deferrable=True,
            initially="DEFERRED",
        )
        op.create_foreign_key(
            "fk_turn_user_message",
            "conversation_turns",
            "conversation_messages",
            ["turn_id", "user_message_id"],
            ["turn_id", "message_id"],
            deferrable=True,
            initially="DEFERRED",
        )


def downgrade():
    """Explicitly remove the application schema."""
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint("fk_turn_current_command", "conversation_turns", type_="foreignkey")
        op.drop_constraint("fk_turn_user_message", "conversation_turns", type_="foreignkey")
    op.drop_table("notification_deliveries")
    op.drop_table("notification_events")
    op.drop_table("interactions")
    op.drop_table("turn_commands")
    op.drop_table("notification_targets")
    op.drop_table("model_context_manifests")
    op.drop_table("conversation_messages")
    op.drop_table("conversation_turns")
    op.drop_table("channel_conversation_bindings")
    op.drop_table("outbox_events")
    op.drop_table("notification_senders")
    op.drop_table("conversations")
    op.drop_table("audit_records")
    op.drop_table("artifacts")
