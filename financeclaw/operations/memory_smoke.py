"""记忆能力（HITL 审批、跨 thread 召回、Manifest 与 Audit）冒烟命令（Stage-3）。

基于真实持久化组件与本地 Agent Server：先提交记忆写入 Turn 并等待 HITL
审批中断，批准后完成写入；再在新会话（新 thread）中触发跨 thread 召回，
校验回复内容、Model Context Manifest 与记忆生命周期 Audit 记录。运行方式：
``python -m financeclaw.operations.memory_smoke``。
"""

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
    """轮询 run 状态直至进入期望状态集合。

    Args:
        service: 会话服务，用于查询 run 状态。
        run_id: 待轮询的 run ID。
        tenant_id: 租户 ID。
        subject_id: 主体 ID。
        expected: 视为成功的状态集合（如 ``{"interrupted"}``）。
        timeout_seconds: 轮询超时秒数。

    Returns:
        进入的期望状态响应。

    Raises:
        RuntimeError: run 以 failed 终态结束。
        TimeoutError: 超时仍未进入期望状态。

    """
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
    """执行一次记忆冒烟：写入审批、跨 thread 召回、Manifest 与审计校验。

    Args:
        url: Agent Server 基础地址。
        database_url: 业务数据库连接串。
        artifact_root: 工件存储根目录。
        timeout_seconds: 每次状态轮询的超时秒数。

    Returns:
        含两次会话 ID、审批与召回结果、记忆 ID、清单与审计数量的摘要。

    Raises:
        RuntimeError: 持久化组件缺失、审批后未完成、未召回记忆或审计不全。
        TimeoutError: run 在超时窗口内未到达期望状态。

    """
    # 1. 构建离线模型、启用持久化的组件装配，并确认会话与审计仓储类型。
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
    # 2. 构造会话服务，并用随机 smoke_id 生成隔离的租户、主体与记忆作用域。
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
        # 3. 创建源会话并提交记忆写入 Turn，等待其进入 HITL 审批中断态。
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
        # 4. 批准写入并确认 Turn 完成。
        approved = await service.resume(
            write.run_id,
            ApprovalDecision(type="approve"),
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
        )
        if approved.status != "completed":
            raise RuntimeError("approved memory write did not complete")
        # 5. 新建会话（新 thread）提交召回请求，等待完成。
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
        # 6. 校验最后一条回复召回内容，且 Manifest 记录了被用到的记忆 ID。
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
        # 7. 校验审计包含 MEMORY_PROPOSED 与 MEMORY_COMMITTED 生命周期事件。
        events = components.audit.records(tenant_id=tenant_id, subject_id=subject_id)
        event_types = {record.event_type for record in events}
        if not {
            AuditEventType.MEMORY_PROPOSED,
            AuditEventType.MEMORY_COMMITTED,
        }.issubset(event_types):
            raise RuntimeError("memory lifecycle Audit records are incomplete")
        # 8. 汇总冒烟结果。
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
    """解析命令行参数并执行一次记忆冒烟，输出 JSON 摘要。"""
    # 1. 解析命令行参数（Server 地址、数据库、工件目录与轮询超时）。
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:2024")
    parser.add_argument(
        "--database-url",
        default="sqlite+pysqlite:///./.financeclaw/financeclaw.db",
    )
    parser.add_argument("--artifact-root", default=".financeclaw/stage3-smoke-artifacts")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    # 2. 执行冒烟探针。
    result = asyncio.run(
        probe_memory(
            url=args.url,
            database_url=args.database_url,
            artifact_root=args.artifact_root,
            timeout_seconds=args.timeout_seconds,
        )
    )
    # 3. 打印 JSON 结果。
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
