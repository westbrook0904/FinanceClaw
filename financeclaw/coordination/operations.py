"""python -m financeclaw.coordination.operations：只读盘点与显式 CAS 运维入口。"""

import argparse
import asyncio
import json
from hashlib import sha256
from pathlib import Path

from financeclaw.coordination.backends.langgraph_backend import LangGraphBackend
from financeclaw.coordination.backends.langgraph_migration import LangGraphLegacyInspector
from financeclaw.coordination.bootstrap import build_coordination
from financeclaw.coordination.deployment import DeploymentControl, diagnostics
from financeclaw.coordination.migration import LegacyMigration


def parser():
    """不提供 HTTP 运维写接口；操作员使用受控数据库凭据执行有摘要的命令。"""
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="action", required=True)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--after", default="")
    inventory.add_argument("--limit", type=int, default=100)
    shadow = commands.add_parser("shadow")
    shadow.add_argument("run_id")
    adopt = commands.add_parser("adopt")
    adopt.add_argument("run_id")
    adopt.add_argument("--fingerprint", required=True)
    adopt.add_argument("--shadow-hash", required=True)
    adopt.add_argument("--control-revision", type=int, required=True)
    commands.add_parser("diagnostics")
    commands.add_parser("control")
    change = commands.add_parser("set-control")
    change.add_argument("--revision", type=int, required=True)
    change.add_argument("--admission-paused", action=argparse.BooleanOptionalAction, required=True)
    change.add_argument("--dispatch-paused", action=argparse.BooleanOptionalAction, required=True)
    change.add_argument("--stopped-evidence", type=Path)
    return result


async def execute(args, services):
    """变更由明确子命令触发；盘点与诊断不读取任何远程执行。"""
    store = services.background_repository
    control = DeploymentControl(store)
    if args.action == "control":
        return control.view()
    if args.action == "diagnostics":
        return diagnostics(store)
    if args.action == "set-control":
        evidence_hash = None
        if args.stopped_evidence:
            evidence = args.stopped_evidence.read_bytes()
            proof = json.loads(evidence)
            if (
                not all(
                    proof.get(key) is True
                    for key in (
                        "legacy_bff_stopped",
                        "legacy_channels_stopped",
                        "legacy_recovery_stopped",
                        "incompatible_coordinators_stopped",
                        "restart_prevented",
                        "agent_runtime_compatible",
                    )
                )
                or not proof.get("deployment_id")
                or not proof.get("recorded_at")
            ):
                raise ValueError(
                    "stopped evidence must identify the deployment and attest all producers"
                )
            evidence_hash = sha256(evidence).hexdigest()
        return control.change(
            args.revision,
            admission_paused=args.admission_paused,
            dispatch_paused=args.dispatch_paused,
            stopped_evidence_hash=evidence_hash,
        )
    backend = LangGraphBackend(services.resources.settings, store, services.background_releases)
    migration = LegacyMigration(
        store, services.background_releases, LangGraphLegacyInspector(backend)
    )
    if args.action == "inventory":
        return migration.inventory(after=args.after, limit=args.limit)
    if args.action == "shadow":
        plan = await migration.shadow(args.run_id)
        return {
            **migration.public(plan),
            "shadow_hash": plan.get("shadow_hash"),
            "control_revision": control.view()["revision"],
        }
    return await migration.adopt(
        args.run_id,
        fingerprint=args.fingerprint,
        shadow_hash=args.shadow_hash,
        control_revision=args.control_revision,
    )


async def main():
    """资源始终释放；不输出异常载荷中的用户内容、请求头或数据库 URL。"""
    args = parser().parse_args()
    services = None
    try:
        services = build_coordination()
        print(json.dumps(await execute(args, services), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error_type": type(exc).__name__,
                    "next_step": "review control, inventory and shadow; do not reset operations",
                }
            )
        )
        raise SystemExit(1) from None
    finally:
        if services:
            services.resources.database.close()


if __name__ == "__main__":
    asyncio.run(main())
