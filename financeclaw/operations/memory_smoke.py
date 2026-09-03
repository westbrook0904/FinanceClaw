"""提供 memory smoke 运维命令的可调用入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import uuid4

from pydantic import SecretStr

from financeclaw.application import ConversationService
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.infrastructure.clients import LangGraphAgentServerClient
from financeclaw.kernel import ApprovalDecision, ConversationTurnRequest
from financeclaw.modules.audit import AuditEventType, SqlAlchemyAuditRepository
from financeclaw.modules.conversation import SqlAlchemyConversationRepository


async def _wait_for(
    service: ConversationService,
    run_id: str,
    *,
    tenant_id: str,
    subject_id: str,
    expected: set[str],
    timeout_seconds: float,
):
    """轮询运行状态直到命中预期状态或超过冒烟测试期限。"""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        status = await service.status(run_id, tenant_id=tenant_id, subject_id=subject_id)
        if status.status in expected:
            return status
        if status.status == "failed":
            raise RuntimeError(f"memory smoke run failed: {status.model_dump(mode='json')!r}")
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("memory smoke run did not reach the expected state")
        await asyncio.sleep(0.2)


async def probe_memory(
    *,
    url: str,
    database_url: str,
    artifact_root: str,
    timeout_seconds: float,
) -> dict[str, object]:
    """通过真实会话验证记忆提议、确认、检索和跨轮注入。"""
    settings = FinanceClawSettings(
        environment="test",
        offline_model=True,
        debug_full_io=False,
        database_url=SecretStr(database_url),
        artifact_root=artifact_root,
    )
    components = build_components(settings, enable_persistence=True)
    repository = components.conversation_repository
    if not isinstance(repository, SqlAlchemyConversationRepository):
        raise RuntimeError("persistent Conversation Journal is unavailable")
    if not isinstance(components.audit, SqlAlchemyAuditRepository):
        raise RuntimeError("persistent AuditRepository is unavailable")
    client = LangGraphAgentServerClient(url=url)
    service = ConversationService(
        client,
        repository,
        components.agent_profiles,
        summary_service=components.summary_service,
        approval_timeout_seconds=settings.approval_timeout_seconds,
    )
    smoke_id = uuid4().hex
    tenant_id = f"stage3-smoke-{smoke_id}"
    subject_id = f"stage3-smoke-{smoke_id}"
    scopes = frozenset({"memory:read", "memory:write", "memory:delete"})
    try:
        source = await service.create(tenant_id=tenant_id, subject_id=subject_id)
        write = await service.start_turn(
            source.conversation_id,
            ConversationTurnRequest(message="remember preference low volatility"),
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            idempotency_key=f"stage3-memory-write-{smoke_id}",
        )
        await _wait_for(
            service,
            write.run_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            expected={"interrupted"},
            timeout_seconds=timeout_seconds,
        )
        approved = await service.resume(
            write.run_id,
            ApprovalDecision(type="approve"),
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
        )
        if approved.status != "completed":
            raise RuntimeError("approved memory write did not complete")

        recall_conversation = await service.create(tenant_id=tenant_id, subject_id=subject_id)
        recall = await service.start_turn(
            recall_conversation.conversation_id,
            ConversationTurnRequest(message="回忆偏好：低波动"),
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            idempotency_key=f"stage3-memory-recall-{smoke_id}",
        )
        await _wait_for(
            service,
            recall.run_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            expected={"completed"},
            timeout_seconds=timeout_seconds,
        )
        messages = service.messages(
            recall_conversation.conversation_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
        )
        manifests = repository.list_manifests(recall_conversation.conversation_id)
        memory_ids = tuple(
            dict.fromkeys(memory_id for manifest in manifests for memory_id in manifest.memory_ids)
        )
        if not memory_ids or "低波动" not in messages.messages[-1].content:
            raise RuntimeError("cross-thread memory was not recalled and manifested")
        events = components.audit.records(tenant_id=tenant_id, subject_id=subject_id)
        event_types = {record.event_type for record in events}
        if not {
            AuditEventType.MEMORY_PROPOSED,
            AuditEventType.MEMORY_COMMITTED,
        }.issubset(event_types):
            raise RuntimeError("memory lifecycle Audit records are incomplete")
        return {
            "source_conversation_id": source.conversation_id,
            "recall_conversation_id": recall_conversation.conversation_id,
            "write_interrupted": True,
            "write_approved": True,
            "memory_ids": memory_ids,
            "manifest_count": len(manifests),
            "audit_count": len(events),
            "cross_thread_recall": True,
        }
    finally:
        if components.database is not None:
            components.database.close()


def main() -> None:
    """解析命令行参数，执行 memory smoke 操作并输出结果。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:2024")
    parser.add_argument(
        "--database-url",
        default="sqlite+pysqlite:///./.financeclaw/financeclaw.db",
    )
    parser.add_argument("--artifact-root", default=".financeclaw/stage3-smoke-artifacts")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    result = asyncio.run(
        probe_memory(
            url=args.url,
            database_url=args.database_url,
            artifact_root=args.artifact_root,
            timeout_seconds=args.timeout_seconds,
        )
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
