"""跨 Agent Server 重启的会话冒烟命令（Stage-2 持久化联调）。

基于真实持久化的 Conversation Journal 与本地 Agent Server，分两次运行验证
会话与 Turn 的持久化：第一次不传 ``--conversation-id`` 创建新会话并提交
Turn，第二次传回同一 ``--conversation-id`` 与新的 ``--idempotency-key`` 在
同一会话继续发言，模拟 Server 重启后的续聊。运行方式：
``python -m financeclaw.operations.conversation_smoke --idempotency-key <键>``。
"""

import argparse
import asyncio
import json

from pydantic import SecretStr

from financeclaw.application import ConversationService
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.infrastructure.clients import LangGraphAgentServerClient
from financeclaw.kernel import ConversationTurnRequest
from financeclaw.modules.conversation import SqlAlchemyConversationRepository


async def probe_conversation(
    *,
    url: str,
    database_url: str,
    artifact_root: str,
    conversation_id: str | None,
    idempotency_key: str,
    message: str,
    timeout_seconds: float,
) -> dict[str, object]:
    """对指定 Agent Server 执行一次会话冒烟探针，返回可校验的结果摘要。

    Args:
        url: Agent Server 基础地址（LangGraph 平台 API）。
        database_url: 会话 Journal 的 SQLAlchemy 连接串。
        artifact_root: 工件存储根目录。
        conversation_id: 续聊的既有会话 ID；为 None 时创建新会话。
        idempotency_key: 本次 Turn 的幂等键，两次运行必须不同。
        message: 发送给顶层 Agent 的消息文本。
        timeout_seconds: 轮询 Turn 完成的超时秒数。

    Returns:
        含会话与线程 ID、消息与清单数量、Agent Profile 版本的摘要字典。

    Raises:
        RuntimeError: 持久化会话仓储不可用，或 Turn 以失败终态结束。
        TimeoutError: Turn 在超时窗口内未到达终态。

    """
    # 1. 构建离线模型、启用持久化的组件装配，并确认会话仓储为 SQLAlchemy 实现。
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
    # 2. 构造 Agent Server 客户端与会话服务。
    client = LangGraphAgentServerClient(url=url)
    service = ConversationService(
        client,
        repository,
        components.agent_profiles,
        summary_service=components.summary_service,
    )
    try:
        # 3. 未指定会话 ID 时创建新会话，否则同步读取既有会话以验证持久化。
        if conversation_id is None:
            conversation = await service.create(
                tenant_id="stage2-smoke",
                subject_id="stage2-smoke",
            )
            conversation_id = conversation.conversation_id
        else:
            conversation = service.get(
                conversation_id,
                tenant_id="stage2-smoke",
                subject_id="stage2-smoke",
            )
        # 4. 提交 Turn：固定租户与主体，授予只读作用域并携带幂等键。
        accepted = await service.start_turn(
            conversation_id,
            ConversationTurnRequest(message=message),
            tenant_id="stage2-smoke",
            subject_id="stage2-smoke",
            scopes=frozenset({"market:read", "tools:read", "artifacts:read"}),
            idempotency_key=idempotency_key,
        )
        # 5. 轮询 Turn 状态直至 completed/failed，超过时限判冒烟失败。
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            status = await service.status(
                accepted.run_id,
                tenant_id="stage2-smoke",
                subject_id="stage2-smoke",
            )
            if status.status in {"completed", "failed"}:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("conversation run did not complete before smoke timeout")
            await asyncio.sleep(0.2)
        # 6. 终态非 completed 视为运行失败。
        if status.status != "completed":
            raise RuntimeError(f"conversation run failed: {status.model_dump(mode='json')!r}")
        # 7. 读取消息、上下文清单与 Agent Profile 版本，汇总冒烟结果。
        messages = service.messages(
            conversation_id,
            tenant_id="stage2-smoke",
            subject_id="stage2-smoke",
        )
        manifests = repository.list_manifests(conversation_id)
        return {
            "conversation_id": conversation_id,
            "thread_id": accepted.thread_id,
            "run_id": accepted.run_id,
            "message_count": len(messages.messages),
            "manifest_count": len(manifests),
            "agent_profile_version": repository.get_owned(
                conversation_id, "stage2-smoke", "stage2-smoke"
            ).agent_profile_version,
            "completed": True,
        }
    finally:
        if components.database is not None:
            components.database.close()


def main() -> None:
    """解析命令行参数并执行一次跨重启会话冒烟，输出 JSON 摘要。"""
    # 1. 解析命令行参数（Server 地址、数据库、工件目录、会话 ID、幂等键等）。
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:2024")
    parser.add_argument(
        "--database-url",
        default="sqlite+pysqlite:///./.financeclaw/stage2-smoke.db",
    )
    parser.add_argument("--artifact-root", default=".financeclaw/stage2-smoke-artifacts")
    parser.add_argument("--conversation-id")
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--message", default="read AAPL")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    # 2. 执行冒烟探针。
    result = asyncio.run(
        probe_conversation(
            url=args.url,
            database_url=args.database_url,
            artifact_root=args.artifact_root,
            conversation_id=args.conversation_id,
            idempotency_key=args.idempotency_key,
            message=args.message,
            timeout_seconds=args.timeout_seconds,
        )
    )
    # 3. 打印 JSON 结果。
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
