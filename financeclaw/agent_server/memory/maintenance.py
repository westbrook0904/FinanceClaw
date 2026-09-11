"""受控数据保留操作；默认仅预览，不修改业务数据。"""

import argparse
import json

from langgraph_sdk import get_sync_client

from financeclaw.shared.conversation.indexing import requeue_history
from financeclaw.shared.conversation.lifecycle import ConversationRetention
from financeclaw.shared.infrastructure.resources import build_resources
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


def main():
    """执行显式回收；运维身份来自部署配置，不接受模型提供的 URL 或存储路径。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("artifacts", "checkpoints", "history"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--strategy", choices=("keep_latest", "delete"), default="keep_latest")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--conversation-id")
    parser.add_argument("--tenant-id")
    parser.add_argument("--subject-id")
    args = parser.parse_args()
    settings = FinanceClawSettings()
    resources = build_resources(settings, enable_persistence=True)
    retention = ConversationRetention(
        resources.database.session_factory, resources.artifact_service.store
    )
    try:
        if args.operation == "artifacts":
            result = retention.cleanup_artifacts(limit=args.limit, apply=args.apply)
        elif args.operation == "history":
            if not all((args.conversation_id, args.tenant_id, args.subject_id)):
                parser.error("history requires conversation, tenant and subject IDs")
            result = requeue_history(
                resources.database.session_factory,
                conversation_id=args.conversation_id,
                tenant_id=args.tenant_id,
                subject_id=args.subject_id,
                limit=args.limit,
                offset=args.offset,
                apply=args.apply,
            )
        else:
            if not all((args.conversation_id, args.tenant_id, args.subject_id)):
                parser.error("checkpoints requires conversation, tenant and subject IDs")
            headers = (
                {
                    "Authorization": "Bearer "
                    + settings.agent_server_service_token.get_secret_value()
                }
                if settings.agent_server_service_token
                else None
            )
            client = get_sync_client(url=settings.agent_server_url, headers=headers)
            result = retention.prune_checkpoints(
                client,
                conversation_id=args.conversation_id,
                tenant_id=args.tenant_id,
                subject_id=args.subject_id,
                apply=args.apply,
                strategy=args.strategy,
            )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        resources.database.close()


if __name__ == "__main__":
    main()
