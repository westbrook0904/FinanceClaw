"""业务引用保护：原生 checkpoint 回收与工件保留共用同一安全边界。"""

from datetime import UTC, datetime

from sqlalchemy import select, update

from financeclaw.shared.artifacts.tables import ArtifactMetadataRow
from financeclaw.shared.conversation.tables import ConversationRow
from financeclaw.shared.turns.tables import ConversationTurnRow, InteractionRow, TurnCommandRow


def has_open_responsibility(session, conversation_id: str | None) -> bool:
    """未知会话不推断终态；活动 Turn、审批和未确认的出站命令都阻止回收。"""
    if not conversation_id:
        return True
    turn_ids = select(ConversationTurnRow.turn_id).where(
        ConversationTurnRow.conversation_id == conversation_id
    )
    checks = (
        select(ConversationTurnRow.turn_id).where(
            ConversationTurnRow.conversation_id == conversation_id,
            ConversationTurnRow.status.not_in(("completed", "failed", "cancelled")),
        ),
        select(InteractionRow.interaction_id).where(
            InteractionRow.turn_id.in_(turn_ids), InteractionRow.status == "pending"
        ),
        select(TurnCommandRow.command_id).where(
            TurnCommandRow.turn_id.in_(turn_ids),
            TurnCommandRow.state.in_(("prepared", "sending", "submitted", "uncertain")),
        ),
    )
    return any(session.scalar(statement.limit(1)) is not None for statement in checks)


class ConversationRetention:
    """仅提供显式运维入口；不配置可能删除待恢复 thread 的全局 TTL。"""

    def __init__(self, sessions, artifact_store):
        """注入业务数据库和已有工件后端。"""
        self.sessions = sessions
        self.artifact_store = artifact_store

    def cleanup_artifacts(self, *, limit: int = 100, apply: bool = False) -> list[dict]:
        """分页回收到期内容，保留目录元数据；数据库提交失败可安全重试。"""
        if not 1 <= limit <= 1000:
            raise ValueError("invalid cleanup batch size")
        now = datetime.now(UTC)
        results = []
        with self.sessions() as session:
            identities = list(
                session.scalars(
                    select(ArtifactMetadataRow.artifact_id)
                    .where(
                        ArtifactMetadataRow.expires_at <= now,
                        ArtifactMetadataRow.deleted_at.is_(None),
                    )
                    .order_by(ArtifactMetadataRow.expires_at)
                    .limit(limit)
                )
            )
        for identity in identities:
            with self.sessions.begin() as session:
                artifact = session.get(ArtifactMetadataRow, identity)
                if artifact is None or artifact.deleted_at is not None:
                    continue
                # 与 begin_turn 的会话锁一致，避免检查完引用后新运行进入。
                session.execute(
                    update(ConversationRow)
                    .where(ConversationRow.conversation_id == artifact.conversation_id)
                    .values(status=ConversationRow.status, updated_at=ConversationRow.updated_at)
                )
                protected = has_open_responsibility(session, artifact.conversation_id)
                status = "protected" if protected else "eligible"
                if apply and not protected:
                    self.artifact_store.delete(artifact.storage_uri)
                    artifact.deleted_at = now
                    status = "deleted"
                results.append({"artifact_id": identity, "status": status})
        return results

    def checkpoint_candidates(self, *, conversation_id: str, tenant_id: str, subject_id: str):
        """Read settled archived threads; product admission never reopens an archive."""
        with self.sessions() as session:
            conversation = session.scalar(
                select(ConversationRow).where(
                    ConversationRow.conversation_id == conversation_id,
                    ConversationRow.tenant_id == tenant_id,
                    ConversationRow.subject_id == subject_id,
                )
            )
            if conversation is None:
                raise LookupError("conversation was not found for owner")
            if conversation.status != "archived" or has_open_responsibility(
                session, conversation_id
            ):
                raise ValueError("checkpoint retention requires an archived, settled conversation")
            threads = {conversation.agent_thread_id}
            threads.update(
                session.scalars(
                    select(ConversationTurnRow.thread_id).where(
                        ConversationTurnRow.conversation_id == conversation_id
                    )
                )
            )
            return sorted(threads)
