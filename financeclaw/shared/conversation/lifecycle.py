"""业务引用保护：原生 checkpoint 回收与工件保留共用同一安全边界。"""

from datetime import UTC, datetime

from sqlalchemy import select, update

from financeclaw.shared.artifacts.tables import ArtifactMetadataRow
from financeclaw.shared.conversation.tables import ConversationRow, ConversationTurnRow
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.run_tables import RootRunRow
from financeclaw.shared.execution_ledger.tables import RunExecutionRow, RunOperationRow


def has_open_responsibility(session, conversation_id: str | None) -> bool:
    """未知会话不推断终态；活动 Turn、审批和未确认的出站命令都阻止回收。"""
    if not conversation_id:
        return True
    run_ids = select(ConversationTurnRow.run_id).where(
        ConversationTurnRow.conversation_id == conversation_id
    )
    checks = (
        select(RootRunRow.run_id).where(
            RootRunRow.conversation_id == conversation_id, RootRunRow.active.is_(True)
        ),
        select(ConversationTurnRow.run_id).where(
            ConversationTurnRow.conversation_id == conversation_id,
            ConversationTurnRow.status.not_in(("completed", "failed", "cancelled")),
        ),
        select(PendingInteractionRow.interaction_id).where(
            PendingInteractionRow.conversation_id == conversation_id,
            PendingInteractionRow.status.in_(("pending", "decided")),
        ),
        select(RunOperationRow.operation_id).where(
            RunOperationRow.run_id.in_(run_ids),
            RunOperationRow.status.in_(("prepared", "claimed", "submitted", "uncertain")),
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

    def prune_checkpoints(
        self,
        client,
        *,
        conversation_id: str,
        tenant_id: str,
        subject_id: str,
        apply: bool = False,
        strategy: str = "keep_latest",
    ) -> dict:
        """只回收归档会话的旧 checkpoint；业务及服务端任一待办都阻止调用。"""
        if strategy not in {"keep_latest", "delete"}:
            raise ValueError("unsupported checkpoint strategy")
        with self.sessions.begin() as session:
            conversation = session.scalar(
                select(ConversationRow)
                .where(
                    ConversationRow.conversation_id == conversation_id,
                    ConversationRow.tenant_id == tenant_id,
                    ConversationRow.subject_id == subject_id,
                )
                .with_for_update()
            )
            if conversation is None:
                raise LookupError("conversation was not found for owner")
            if conversation.status != "archived" or has_open_responsibility(
                session, conversation_id
            ):
                raise ValueError("checkpoint retention requires an archived, settled conversation")
            snapshots = session.scalars(
                select(RunExecutionRow.snapshot)
                .join(ConversationTurnRow, ConversationTurnRow.run_id == RunExecutionRow.run_id)
                .where(ConversationTurnRow.conversation_id == conversation_id)
            )
            threads = {conversation.agent_thread_id}
            threads.update(value["thread_id"] for value in snapshots if value.get("thread_id"))
            verified = []
            for thread_id in sorted(threads):
                try:
                    thread = client.threads.get(thread_id)
                    state = client.threads.get_state(thread_id, subgraphs=True)
                except Exception as exc:
                    # Unsupported endpoints and missing native state fail closed.
                    raise ValueError("native thread state could not be verified") from exc
                tasks = state.get("tasks", [])
                if (
                    thread.get("status") != "idle"
                    or state.get("next")
                    or any(task.get("interrupts") or task.get("error") for task in tasks)
                ):
                    raise ValueError("native thread still has pending work")
                verified.append(thread_id)
            result = {"threads": verified, "strategy": strategy, "applied": False}
            if apply:
                result["native_result"] = client.threads.prune(verified, strategy=strategy)
                result["applied"] = True
            return result
