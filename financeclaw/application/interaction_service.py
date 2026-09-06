"""统一用户交互：校验发布契约并恢复真正提出问题的 owner，而不是父模型代答。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from financeclaw.kernel import ApprovalDecision, ExecutionContext
from financeclaw.modules.execution import snapshot_context
from financeclaw.modules.execution.repository import digest
from financeclaw.modules.interactions import (
    InteractionConflict,
    InteractionRepository,
    InteractionResponse,
)
from financeclaw.modules.interactions.repository import aware
from financeclaw.orchestration.agents.middleware import redact_sensitive

from .execution_service import ExecutionService, verify_agent_snapshot


class InteractionService:
    """HTTP、兼容 resume 和飞书共用同一决定表与 CAS 出站操作。"""

    def __init__(
        self, client, execution, *, agent_profiles=None, workflow_catalog=None, clock=None
    ):
        """复用业务执行仓储；时钟与发布目录由所在应用服务提供。"""
        self.client, self.execution = client, execution
        self.repository = InteractionRepository(execution)
        self.operations = ExecutionService(client, execution)
        self.agent_profiles, self.workflow_catalog = agent_profiles, workflow_catalog
        self.clock = clock or (lambda: datetime.now(UTC))

    async def observe_agent(
        self,
        run_id: str,
        observation,
        *,
        server_run_id: str,
        expires_at: datetime,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        """只登记原生单 HITL 或 Profile 明确声明的交互点，不接受任意模型 Schema。"""
        execution = await asyncio.to_thread(self.execution.get, run_id)
        profile = self._agent_profile(execution["snapshot"])
        if not observation.interrupt_id:
            raise InteractionConflict(
                "new user interactions require an explicit native interrupt ID"
            )
        payload = observation.payload
        if observation.kind == "hitl":
            action = payload["action_requests"][0]
            config = payload["review_configs"][0]
            if (
                action["name"] not in {ref.tool_id for ref in profile.allowed_tools}
                or config.get("action_name") != action["name"]
            ):
                raise InteractionConflict("approval does not match the pinned Agent tool")
            allowed = [
                item
                for item in config.get("allowed_decisions", ())
                if item in {"approve", "reject"}
            ]
            if not allowed:
                raise InteractionConflict("no supported approval decision")
            point_id, kind, question = (
                action["name"],
                "approval",
                f"请确认是否执行 {action['name']}",
            )
            request = {
                "action": action,
                "action_hash": digest(action),
                "arguments_hash": digest(action),
                "allowed_decisions": allowed,
                "required_scope": None,
                "native_payload": payload,
            }
            source = "agent_hitl"
        else:
            point = next(
                (p for p in profile.interaction_points if p.point_id == payload.get("point_id")),
                None,
            )
            if point is None or payload.get("interaction_kind") != point.kind:
                raise InteractionConflict("Agent returned an unpublished interaction point")
            question = payload.get("question") or point.question
            if not isinstance(question, str) or not 1 <= len(question) <= 2000:
                raise InteractionConflict("interaction question is invalid")
            point_id, kind, source = point.point_id, point.kind, "agent_declared"
            request = {
                "declaration": point.model_dump(mode="json"),
                "options": list(point.options),
                "response_schema": point.response_schema,
                "required_scope": point.required_scope,
                "native_payload": payload,
            }
            if kind == "approval":
                action = payload.get("action")
                if (
                    not isinstance(action, dict)
                    or not action
                    or len(json.dumps(action).encode()) > 16384
                ):
                    raise InteractionConflict(
                        "declared approval requires a concrete action snapshot"
                    )
                request.update(
                    action=action,
                    action_hash=digest(action),
                    arguments_hash=digest(action),
                    allowed_decisions=["approve", "reject"],
                )
            expires_at = min(expires_at, self.clock() + timedelta(seconds=point.timeout_seconds))
        return await asyncio.to_thread(
            self.repository.register,
            run_id,
            source=source,
            server_run_id=server_run_id,
            interrupt_id=observation.interrupt_id,
            point_id=point_id,
            kind=kind,
            question=question,
            request=request,
            expires_at=expires_at,
            now=self.clock(),
            checkpoint_id=checkpoint_id,
        )

    async def observe_workflow(
        self,
        run_id: str,
        observation,
        approval,
        *,
        server_run_id: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        """原 Workflow 验证通过后映射交互；决定时原审批单与交互在同一事务更新。"""
        if not observation.interrupt_id:
            raise InteractionConflict("new Workflow interactions require a native interrupt ID")
        payload = approval.request_payload
        return await asyncio.to_thread(
            self.repository.register,
            run_id,
            source="workflow_approval",
            server_run_id=server_run_id,
            interrupt_id=observation.interrupt_id,
            point_id=approval.approval_point,
            kind="approval",
            question=f"请确认 {approval.requested_action}",
            request={
                "approval_id": approval.approval_id,
                "action": payload,
                "action_hash": digest(payload),
                "arguments_hash": approval.arguments_hash,
                "allowed_decisions": list(approval.allowed_decisions),
                "required_scope": approval.required_scope,
                "native_payload": payload,
            },
            expires_at=aware(approval.expires_at),
            now=self.clock(),
            checkpoint_id=checkpoint_id,
        )

    def _agent_profile(self, snapshot):
        """定位实际发布版本，禁止同名 Agent 在旧检查点恢复时静默升级。"""
        saved = snapshot["profile"]
        if self.agent_profiles is None:
            raise InteractionConflict("Agent release resolver is unavailable")
        try:
            profile = self.agent_profiles.resolve(saved["agent_id"], saved["version"])
        except LookupError as exc:
            raise InteractionConflict("pinned Agent release is unavailable") from exc
        verify_agent_snapshot(profile, snapshot)
        return profile

    def _validate_release(self, snapshot):
        """回答前再次验证执行发布，返回原 owner 的执行权限要求。"""
        if "profile" in snapshot:
            return self._agent_profile(snapshot).required_scopes
        from .workflow_service import WorkflowService

        release = snapshot["release"]
        if self.workflow_catalog is None:
            raise InteractionConflict("Workflow release resolver is unavailable")
        try:
            definition = self.workflow_catalog[(release["workflow_id"], release["version"])]
        except KeyError as exc:
            raise InteractionConflict("pinned Workflow release is unavailable") from exc
        if WorkflowService._release(definition) != release:
            raise InteractionConflict("pinned Workflow release is unavailable")
        return definition.required_scopes

    @staticmethod
    def _validate_response(row, response):
        """分型校验回答及动作摘要，Schema 错误不返回用户输入原文。"""
        if response.kind != row["kind"] or response.revision != row["revision"]:
            raise InteractionConflict("response kind or revision does not match")
        request = row["request"]
        if response.kind == "approval":
            if (
                response.action_hash != request["action_hash"]
                or response.decision not in request["allowed_decisions"]
            ):
                raise InteractionConflict("approval does not match the immutable action snapshot")
        elif response.kind == "choice":
            if not isinstance(response.answer, str) or response.answer not in request["options"]:
                raise InteractionConflict("answer is not a published choice")
        else:
            try:
                Draft202012Validator(request["response_schema"]).validate(response.answer)
            except ValidationError as exc:
                raise InteractionConflict(
                    "answer does not satisfy the declared response schema"
                ) from exc

    async def respond(
        self,
        interaction_id: str,
        response: InteractionResponse,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
        idempotency_key: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """先保存决定和操作，再提交；同决定重放不追加 Journal，也不会恢复父检查点。"""
        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise InteractionConflict("a bounded response idempotency key is required")
        row = await asyncio.to_thread(
            self.repository.get_owned, interaction_id, tenant_id, subject_id, now=self.clock()
        )
        if conversation_id is not None and row["conversation_id"] != conversation_id:
            raise InteractionConflict("interaction belongs to a different channel conversation")
        self._validate_response(row, response)
        snapshot = (await asyncio.to_thread(self.execution.get, row["owner_run_id"]))["snapshot"]
        required = self._validate_release(snapshot)
        context = snapshot_context(snapshot, scopes)
        approval_scope = row["request"].get("required_scope")
        if (
            ("*" not in scopes and approval_scope and approval_scope not in scopes)
            or ("*" not in context.scopes and not required.issubset(context.scopes))
            or not context.scopes
        ):
            raise InteractionConflict("current authorization does not permit this response")
        if row["source"] in {"agent_hitl", "workflow_approval"}:
            mapped = {"type": response.decision}
            if response.reason:
                mapped["message"] = response.reason
            if row["source"] == "workflow_approval":
                mapped["arguments_hash"] = row["request"]["arguments_hash"]
            value = {"decisions": [mapped]}
        else:
            value = {
                "kind": response.kind,
                "answer": response.answer,
                "decision": response.decision,
                "action_hash": response.action_hash,
                "reason": response.reason,
            }
        operation = {
            "thread_id": row["thread_id"],
            "assistant_id": snapshot["assistant_id"],
            "input": None,
            "command": {"resume": {row["interrupt_id"]: value}},
            "predecessor": row["server_run_id"],
            "context": context.model_dump(mode="json"),
            "metadata": {
                "business_run_id": row["owner_run_id"],
                "root_run_id": row["root_run_id"],
                "parent_run_id": row["parent_run_id"],
                "interaction_id": interaction_id,
                "stage": "6fix-c",
            },
        }
        decided = await asyncio.to_thread(
            self.repository.decide,
            interaction_id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            revision=response.revision,
            response_key=idempotency_key,
            response=response.model_dump(mode="json"),
            operation=operation,
            now=self.clock(),
        )
        root = await asyncio.to_thread(self.execution.get, row["root_run_id"])
        # 已受理决定在取消后仍可幂等查询，但不能因此重新派发。
        if not root["cancellation_requested"]:
            saved_op = await asyncio.to_thread(self.execution.operation, decided["operation_id"])
            saved_context = ExecutionContext.model_validate(saved_op["request"]["context"])
            if "*" not in context.scopes and not saved_context.scopes.issubset(context.scopes):
                raise InteractionConflict("authorization was narrowed after response acceptance")
            if saved_op["status"] != "prepared" or self.clock() < aware(decided["expires_at"]):
                await self.operations.submit_prepared(decided["operation_id"])
        return await self.public(decided)

    async def resume_legacy(
        self,
        run_id: str,
        decision: ApprovalDecision,
        *,
        tenant_id: str,
        subject_id: str,
        scopes: frozenset[str],
    ) -> bool:
        """兼容旧单审批入口；资料回答永不通过 ApprovalDecision 路径。"""
        rows = await asyncio.to_thread(self.repository.for_owner, run_id)
        candidates = [row for row in rows if row["interrupt_id"] == decision.interrupt_id]
        if not rows:
            return False
        # 已提交的回答仍按原 ID 幂等查询，不能因最新 server run 已前进而
        # 回退到旧审批路径，再准备第二个恢复操作。
        if len(candidates) != 1 or candidates[0]["kind"] != "approval":
            raise InteractionConflict("use the explicit interaction response endpoint")
        row = candidates[0]
        if (
            decision.interrupt_id != row["interrupt_id"]
            or decision.arguments_hash != row["request"]["arguments_hash"]
        ):
            raise InteractionConflict("legacy approval does not match the exact interaction")
        if decision.type.value not in {"approve", "reject"}:
            raise InteractionConflict("changed actions require a new request and confirmation")
        await self.respond(
            row["interaction_id"],
            InteractionResponse(
                revision=row["revision"],
                kind="approval",
                decision=decision.type.value,
                action_hash=row["request"]["action_hash"],
                reason=decision.reason,
            ),
            tenant_id=tenant_id,
            subject_id=subject_id,
            scopes=scopes,
            idempotency_key="legacy:" + row["interaction_id"],
        )
        return True

    async def public(self, row) -> dict[str, Any]:
        """仅输出问题、动作安全投影与关联字段，不泄露内部检查点、Schema 外状态或原回答。"""
        row = await asyncio.to_thread(
            self.repository.get_owned,
            row["interaction_id"],
            row["tenant_id"],
            row["subject_id"],
            now=self.clock(),
        )
        request = row["request"]
        projection = {
            key: row[key]
            for key in (
                "interaction_id",
                "revision",
                "kind",
                "status",
                "question",
                "root_run_id",
                "owner_run_id",
                "interrupt_id",
            )
        }
        projection.update(
            expires_at=aware(row["expires_at"]).isoformat(),
            response_url=f"/v1/interactions/{row['interaction_id']}/responses",
        )
        for key in (
            "options",
            "response_schema",
            "action_hash",
            "arguments_hash",
            "allowed_decisions",
            "approval_id",
        ):
            if key in request:
                projection[key] = request[key]
        if "action" in request:
            projection["action"] = redact_sensitive(request["action"])
        if row["operation_id"]:
            operation = await asyncio.to_thread(self.execution.operation, row["operation_id"])
            projection["resume_status"] = operation["status"]
            if operation["status"] == "prepared":
                root = await asyncio.to_thread(self.execution.get, row["root_run_id"])
                if root["cancellation_requested"]:
                    projection["resume_status"] = "cancelled_before_submission"
                elif self.clock() >= aware(row["expires_at"]):
                    projection["resume_status"] = "expired_before_submission"
        return projection

    async def reconcile_owner(self, run_id: str, *, scopes: frozenset[str] | None = None) -> None:
        """恢复已落决定但尚未领取的操作；未知提交只对账，不盲目重发。

        领取前中断可以在带当前权限的查询中推进。后台无当前认证时只观察已经
        受理的尝试；窗口过期而未提交的决定也不会被悄悄延期执行。
        """
        rows = await asyncio.to_thread(self.repository.for_owner, run_id)
        if rows:
            await self.operations.reconcile(run_id)
        for row in rows:
            if not row["operation_id"]:
                continue
            operation = await asyncio.to_thread(self.execution.operation, row["operation_id"])
            root = await asyncio.to_thread(self.execution.get, row["root_run_id"])
            if root["cancellation_requested"]:
                continue
            if operation["status"] == "prepared":
                if scopes is None:
                    continue
                if self.clock() >= aware(row["expires_at"]):
                    continue  # 保留已接受决定，但不自动延期；状态投影说明须取消后重试。
                snapshot = (await asyncio.to_thread(self.execution.get, run_id))["snapshot"]
                self._validate_release(snapshot)
                context = snapshot_context(snapshot, scopes)
                previous = ExecutionContext.model_validate(operation["request"]["context"])
                required_scope = row["request"].get("required_scope")
                if ("*" not in context.scopes and not previous.scopes.issubset(context.scopes)) or (
                    required_scope and "*" not in scopes and required_scope not in scopes
                ):
                    raise InteractionConflict(
                        "current authorization no longer permits the accepted response"
                    )
                await self.operations.submit_prepared(row["operation_id"])
            await self.operations.result(run_id, "interaction:" + row["interaction_id"])


def waiting_reason(interaction: dict[str, Any]) -> str:
    """窗口终态与执行状态分开呈现；超时没有自动停止远程执行的含义。"""
    status = interaction["status"]
    if status == "pending":
        return {
            "approval": "approval_required",
            "input": "input_required",
            "choice": "choice_required",
        }[interaction["kind"]]
    if status in {"resolved", "rejected"}:
        if interaction.get("resume_status") in {
            "cancelled_before_submission",
            "expired_before_submission",
        }:
            return interaction["resume_status"]
        return (
            "submission_uncertain"
            if interaction.get("resume_status") in {"claimed", "uncertain"}
            else "resume_pending"
        )
    return (
        ("approval_expired" if interaction["kind"] == "approval" else "interaction_expired")
        if status == "expired"
        else "interaction_" + status
    )
