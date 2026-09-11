"""受控数据保留操作；默认仅预览，不修改业务数据。"""

import argparse
import json
import os

import httpx

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
            if not args.conversation_id:
                parser.error(
                    "checkpoints requires --conversation-id; ownership comes from the token"
                )
            token = os.environ.get("FINANCECLAW_MAINTENANCE_TOKEN")
            if not token:
                parser.error("set FINANCECLAW_MAINTENANCE_TOKEN with the maintenance scope")
            with httpx.Client(base_url=settings.internal_api_url, timeout=30) as client:
                response = client.post(
                    f"/v1/conversations/{args.conversation_id}/checkpoints/prune",
                    headers={"Authorization": "Bearer " + token},
                    json={"apply": args.apply, "strategy": args.strategy},
                )
                response.raise_for_status()
                result = response.json()
        print(json.dumps(result, ensure_ascii=False))
    finally:
        resources.database.close()


if __name__ == "__main__":
    main()
