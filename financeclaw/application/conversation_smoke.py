"""Live Stage-2 Conversation Journal and Agent Server restart smoke probe."""

import argparse
import asyncio
import json

from pydantic import SecretStr

from financeclaw.bootstrap import build_components
from financeclaw.contracts import ConversationTurnRequest
from financeclaw.conversation import SqlAlchemyConversationRepository
from financeclaw.infrastructure import FinanceClawSettings

from .agent_server_client import LangGraphAgentServerClient
from .conversation_service import ConversationService


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
    client = LangGraphAgentServerClient(url=url)
    service = ConversationService(
        client,
        repository,
        components.agent_profiles,
        summary_service=components.summary_service,
    )
    try:
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
        accepted = await service.start_turn(
            conversation_id,
            ConversationTurnRequest(message=message),
            tenant_id="stage2-smoke",
            subject_id="stage2-smoke",
            scopes=frozenset({"market:read", "tools:read", "artifacts:read"}),
            idempotency_key=idempotency_key,
        )
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
        if status.status != "completed":
            raise RuntimeError(f"conversation run failed: {status.model_dump(mode='json')!r}")
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
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
