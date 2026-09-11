"""单条记忆删除的持久恢复任务；回执不保存记忆正文。"""

import asyncio

from langgraph_sdk.errors import NotFoundError

from financeclaw.shared.audit.models import AuditRecord
from financeclaw.shared.outbox.models import OutboxEvent

MEMORY_DELETE_DESTINATION = "memory_delete"


def deletion_event(record, key: str, audit: AuditRecord) -> OutboxEvent:
    """先记录目标记录的 mutation，后台重试不会删除后来保存的新值。"""
    return OutboxEvent(
        event_id=f"memory-delete:{audit.audit_id}",
        event_type="memory.delete.requested",
        destination=MEMORY_DELETE_DESTINATION,
        aggregate_type="memory",
        aggregate_id=record.memory_id,
        tenant_id=record.tenant_id,
        subject_id=record.subject_id,
        payload={
            "namespace": list(record.namespace),
            "key": key,
            "mutation_id": record.mutation_id,
            "audit": audit.model_dump(mode="json"),
        },
    )


class MemoryDeletionConsumer:
    """通过 Agent Server 的 Store API 补齐删除及幂等审计。"""

    def __init__(self, store_client, audit):
        """注入原生 Store API 与持久审计仓储。"""
        self.store = store_client
        self.audit = audit

    async def publish(self, event: OutboxEvent) -> None:
        """Store 失败继续重试；删除已完成但审计失败时补写同一审计 ID。"""
        if event.destination != MEMORY_DELETE_DESTINATION:
            raise ValueError("wrong memory deletion destination")
        payload = event.payload
        audit = AuditRecord.model_validate(payload["audit"])
        if (audit.tenant_id, audit.subject_id) != (event.tenant_id, event.subject_id):
            raise ValueError("deletion owner mismatch")
        namespace, key = tuple(payload["namespace"]), payload["key"]
        try:
            item = await self.store.get_item(namespace, key, refresh_ttl=False)
        except NotFoundError:
            item = None
        if item and item["value"].get("mutation_id") == payload["mutation_id"]:
            await self.store.delete_item(namespace, key)
        await asyncio.to_thread(self.audit.append, audit)
