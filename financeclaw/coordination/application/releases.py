"""协调用的固定发布解析与子输入构造；不创建模型或执行图。"""

import json
from uuid import uuid4

from financeclaw.coordination.workflows.service import WorkflowService
from financeclaw.kernel.coordination import ReleaseRef, handoff_input
from financeclaw.shared.execution_ledger.authorization import require_scopes
from financeclaw.shared.execution_ledger.repository import (
    ExecutionConflict,
    digest,
    snapshot_context,
)
from financeclaw.shared.execution_ledger.snapshots import agent_snapshot, verify_agent_snapshot


def release_ref(snapshot) -> ReleaseRef:
    """只摘要发布内容；任务身份、thread 与输入不混入发布指纹。"""
    if "profile" in snapshot:
        profile = snapshot["profile"]
        return ReleaseRef(
            kind="agent",
            target_id=profile["agent_id"],
            version=profile["version"],
            fingerprint=digest(
                {key: snapshot[key] for key in ("profile", "input_schema", "output_schema")}
            ),
        )
    release = snapshot["release"]
    return ReleaseRef(
        kind="workflow",
        target_id=release["workflow_id"],
        version=release["version"],
        fingerprint=digest(release),
    )


class CoordinationReleases:
    """复用既有委派准入规则和发布 catalog，固定 child 版本与授权引用。"""

    def __init__(self, agents, workflows, delegation_service):
        """注入纯声明与已有领域校验，不依赖 AgentServer 执行对象。"""
        self.agents, self.workflows, self.delegations = agents, workflows, delegation_service

    def verify(self, snapshot):
        """精确发布消失或改变时停止推进，不能自动选 latest。"""
        try:
            if "profile" in snapshot:
                profile = self.agents.resolve(
                    snapshot["profile"]["agent_id"], snapshot["profile"]["version"]
                )
                verify_agent_snapshot(profile, snapshot)
                return profile
            release = snapshot["release"]
            definition = self.workflows.resolve(release["workflow_id"], release["version"])
            if WorkflowService._release(definition) != release:
                raise ExecutionConflict("pinned Workflow release changed")
            return definition
        except LookupError as exc:
            raise ExecutionConflict("pinned backend release is unavailable") from exc

    def target(self, handoff, parent_snapshot, scopes):
        """校验固定父工具绑定、目标权限、父 Turn 与受治理参数。"""
        self.verify(parent_snapshot)
        context = snapshot_context(parent_snapshot, scopes)
        self.delegations._verify_parent(
            handoff,
            parent_run_id=context.run_id,
            parent_turn_id=context.turn_id,
            conversation_id=context.conversation_id,
        )
        return self.delegations._resolve(handoff, context.scopes, parent_snapshot)

    def child(self, request, parent_snapshot, scopes):
        """在短数据库事务之外解析不可变引用；受理时再复验根取消和授权。"""
        kind, target_id, version, arguments = self.target(request.handoff, parent_snapshot, scopes)
        arguments = handoff_input(request.handoff)
        original = snapshot_context(parent_snapshot)
        child_id, thread_id = str(uuid4()), str(uuid4())
        context = original.model_copy(
            update={
                "run_id": child_id,
                "parent_run_id": original.run_id,
                "root_run_id": original.root_run_id or original.run_id,
                "delegation_id": request.request_id,
            }
        )
        if kind.value == "agent":
            profile = self.agents.resolve(target_id, version)
            values = arguments.get("arguments", {})
            if profile.input_schema:
                values = profile.input_schema.model_validate(values).model_dump(mode="json")
            refs = self.delegations._resolve_context_refs(
                tuple(arguments.get("context_refs", ())), context=context
            )
            snapshot = agent_snapshot(
                profile, context, thread_id=thread_id, input_hash=request.input_hash
            )
            snapshot["resolved_context"] = refs
            payload = {
                "messages": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "task": arguments["task"],
                                "arguments": values,
                                "authorized_context": refs,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            }
        else:
            definition = self.workflows.resolve(target_id, version)
            snapshot = {
                "context": context.model_dump(mode="json"),
                "thread_id": thread_id,
                "assistant_id": definition.assistant_id,
                "input_hash": request.input_hash,
                "release": WorkflowService._release(definition),
                "limits": parent_snapshot["limits"],
            }
            payload = definition.normalize_input(arguments)
            snapshot["input_hash"] = digest(payload)
        snapshot["driver_mode"] = "coordinator"
        snapshot["backend_instance_id"] = parent_snapshot["backend_instance_id"]
        if request.target != release_ref(snapshot):
            raise ExecutionConflict("delegation target release changed")
        require_scopes(scopes, self.verify(snapshot).required_scopes)
        return snapshot, payload, arguments
