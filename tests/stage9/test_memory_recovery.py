"""Store 已变更而审计失败时，重入必须补齐回执并失效旧上下文。"""

from datetime import UTC, datetime, timedelta

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from financeclaw.agent_server.context.compaction import NativeContextMiddleware
from financeclaw.agent_server.memory.models import MemoryDraft, MemoryStatus
from financeclaw.agent_server.memory.service import MemoryReceiptPending
from financeclaw.agent_server.tools.memory import ForgetMemoryTool
from financeclaw.kernel.context import ExecutionContext
from financeclaw.shared.audit.repository import SqlAlchemyAuditRepository
from financeclaw.shared.audit.tables import AuditRecordRow
from financeclaw.shared.memory.deletion import (
    MEMORY_DELETE_DESTINATION,
    MemoryDeletionConsumer,
)
from financeclaw.shared.outbox.repository import SqlAlchemyOutboxRepository
from tests.stage9.test_context import budget
from tests.stage9.test_history_lifecycle import StoreClient
from tests.stage9.test_memory import BoundScript


class FailAuditOnce:
    """只让目标动作的一次审计失败，其他事实仍写入真实数据库。"""

    def __init__(self, repository, action):
        """绑定故障动作与后续用于恢复的真实仓储。"""
        self.repository = repository
        self.action = action
        self.failed = False

    def append(self, record):
        """模拟 Store 成功之后发生的审计连接故障。"""
        if record.action == self.action and not self.failed:
            self.failed = True
            raise ConnectionError("synthetic audit outage")
        self.repository.append(record)


def audit_actions(repository):
    """读取实际持久化回执，确保去重未掩盖正文 hash 冲突。"""
    with repository._sessions() as session:
        return list(session.scalars(select(AuditRecordRow.action)))


def test_revoke_reentry_keeps_original_receipt_and_timestamp(memory_stack):
    """撤销后的重试不改更新时间；真实 SQL 审计接受完全相同的回执。"""
    context, identity, repository, _, service, store = memory_stack
    service.audit = SqlAlchemyAuditRepository(repository._sessions, emit_outbox=False)
    now = datetime.now(UTC)
    service._clock = lambda: now
    record = service.save(
        context,
        store,
        draft=MemoryDraft(kind="goal", content="用户确认的计划", evidence_message_ids=(identity,)),
        mutation_id="event",
        approved=True,
    )
    now += timedelta(seconds=1)
    service.forget(context, store, record.memory_id, mode="revoke")
    first = service.get(context, store, record.memory_id, include_inactive=True)
    now += timedelta(seconds=1)
    service.forget(context, store, record.memory_id, mode="revoke")
    assert service.get(context, store, record.memory_id, include_inactive=True) == first
    assert audit_actions(repository).count("revoke") == 1


def test_replacement_reentry_completes_audit_after_store_changed(memory_stack):
    """旧事件已失效时，重入仍须补齐失败的 supersede 审计。"""
    context, identity, repository, _, service, store = memory_stack
    service.audit = FailAuditOnce(
        SqlAlchemyAuditRepository(repository._sessions, emit_outbox=False), "supersede"
    )
    draft = MemoryDraft(kind="goal", content="原计划", evidence_message_ids=(identity,))
    old = service.save(context, store, draft=draft, mutation_id="old", approved=True)
    arguments = dict(
        draft=draft.model_copy(update={"content": "新计划"}),
        mutation_id="replacement",
        approved=True,
        supersedes_id=old.memory_id,
    )
    with pytest.raises(MemoryReceiptPending):
        service.save(context, store, **arguments)
    assert service.get(context, store, old.memory_id, include_inactive=True).status is (
        MemoryStatus.SUPERSEDED
    )
    saved = service.save(context, store, **arguments)
    assert service.save(context, store, **arguments) == saved
    assert audit_actions(repository).count("supersede") == 1
    assert audit_actions(repository).count("commit") == 2


@pytest.mark.asyncio
async def test_delete_receipt_failure_resets_native_context_and_recovers(memory_stack):
    """删除已成功但审计失败仍重置混合摘要；工具不虚报完成，后台补写回执。"""
    context, identity, repository, artifacts, service, store = memory_stack
    service.outbox = SqlAlchemyOutboxRepository(repository._sessions)
    service.audit = FailAuditOnce(
        SqlAlchemyAuditRepository(repository._sessions, emit_outbox=False), "delete"
    )
    record = service.save(
        context,
        store,
        draft=MemoryDraft(kind="goal", content="旧记忆正文", evidence_message_ids=(identity,)),
        mutation_id="event",
        approved=True,
    )
    graph = create_agent(
        BoundScript(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "forget_memory",
                            "id": "delete-call",
                            "args": {"memory_id": record.memory_id, "mode": "delete"},
                        }
                    ],
                ),
                AIMessage(content="审计回执待补齐"),
            ]
        ),
        tools=[ForgetMemoryTool(service)],
        context_schema=ExecutionContext,
        middleware=[NativeContextMiddleware(budget(), repository, artifacts)],
        store=store,
        checkpointer=InMemorySaver(),
    )
    result = await graph.ainvoke(
        {
            "context_bootstrapped": True,
            "messages": [
                HumanMessage(
                    content="包含旧记忆正文的混合摘要",
                    id="mixed-summary",
                    additional_kwargs={"lc_source": "summarization"},
                ),
                HumanMessage(content="删除这条记忆", id=identity),
            ],
        },
        {"configurable": {"thread_id": "pending-delete"}},
        context=context,
    )
    assert store.get(record.namespace, record.memory_id) is None
    assert all(message.id != "mixed-summary" for message in result["messages"])
    receipt = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    assert receipt.status == "error" and "receipt_pending" in receipt.content
    task = service.outbox.claim_pending(destination=MEMORY_DELETE_DESTINATION, limit=1)[0]
    await MemoryDeletionConsumer(StoreClient(store), service.audit).publish(task)
    assert audit_actions(repository).count("delete") == 1
