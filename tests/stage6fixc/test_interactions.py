"""真实数据库 + 可注入故障的执行平面，验证 C 阶段决定与恢复的边界。"""

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from financeclaw.application import RunService, TargetResolver
from financeclaw.application.interaction_service import InteractionService
from financeclaw.interfaces.http import create_app
from financeclaw.interfaces.http.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from financeclaw.modules.interactions import (
    InteractionConflict,
    InteractionNotFound,
    InteractionResponse,
)
from financeclaw.modules.interactions.tables import PendingInteractionRow
from tests.stage4.test_delegation import SCOPES, FakeDelegationClient
from tests.stage6fix.test_execution_recovery import (
    OWNER,
    ParentApprovalClient,
    stack,
    started_child,
)

SCOPES = SCOPES | {"watchlist:write"}


def reply(item, **kwargs):
    """从服务端投影构造回答，不由客户端重新计算动作摘要。"""
    return InteractionResponse(
        revision=item["revision"],
        kind=item["kind"],
        **(
            {"decision": "approve", "action_hash": item["action_hash"]}
            if item["kind"] == "approval"
            else kwargs
        ),
    )


async def parent_approval(tmp_path):
    """创建真实交付后产生的父审批。"""
    components, fake, delegation, service = stack(tmp_path, ParentApprovalClient())
    accepted, _, child = await started_child(service, fake, scopes=SCOPES)
    child["status"] = "success"
    waiting = await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
    return components, fake, delegation, service, accepted, waiting.pending_interactions[0]


@pytest.mark.asyncio
async def test_concurrent_same_answer_is_one_decision_and_one_resume(tmp_path):
    """C02/C03：并发重放同一回答只产生一条决定、一条出站操作。"""
    components, fake, _, service, accepted, item = await parent_approval(tmp_path)
    responses = await asyncio.gather(
        *(
            service.interactions.respond(
                item["interaction_id"],
                reply(item),
                scopes=SCOPES,
                idempotency_key="same-answer",
                **OWNER,
            )
            for _ in range(12)
        )
    )
    assert all(response["status"] == "resolved" for response in responses)
    assert len(fake.resume_calls) == 2  # 子结果交付 + 一次父审批。
    assert (await service.status(accepted.run_id, scopes=SCOPES, **OWNER)).status == "completed"
    assert len(components.conversation_repository.list_messages(accepted.conversation_id)) == 2
    with components.database.session() as session:
        row = session.scalar(select(PendingInteractionRow))
        assert row.response_key == "same-answer" and row.operation_id
        assert service.execution.operation(row.operation_id)["status"] == "observed"


@pytest.mark.parametrize("fault", ["revision", "hash", "identity", "kind", "revoked"])
@pytest.mark.asyncio
async def test_invalid_or_unauthorized_answers_do_not_submit(tmp_path, fault):
    """版本、动作、身份、类型或当前权限错误都不能产生新的恢复。"""
    _, fake, _, service, _, item = await parent_approval(tmp_path)
    response = reply(item)
    owner, scopes = OWNER, SCOPES
    if fault == "revision":
        response = response.model_copy(update={"revision": item["revision"] + 1})
    if fault == "hash":
        response = response.model_copy(update={"action_hash": "0" * 64})
    if fault == "kind":
        response = InteractionResponse(
            revision=item["revision"], kind="input", answer={"text": "同意"}
        )
    if fault == "identity":
        owner = {"tenant_id": "another", "subject_id": "another"}
    if fault == "revoked":
        scopes = frozenset()
    with pytest.raises((InteractionConflict, InteractionNotFound)):
        await service.interactions.respond(
            item["interaction_id"], response, scopes=scopes, idempotency_key="bad", **owner
        )
    assert len(fake.resume_calls) == 1


@pytest.mark.parametrize("close", ["expired", "cancelled", "superseded"])
@pytest.mark.asyncio
async def test_stale_interaction_is_never_resumed(tmp_path, close):
    """旧命令／卡片只能查到终态，不会自动批准新的动作实例。"""
    _, fake, _, service, accepted, item = await parent_approval(tmp_path)
    if close == "expired":
        service._clock = lambda: datetime.fromisoformat(item["expires_at"]) + timedelta(seconds=1)
        await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
    elif close == "cancelled":
        await service.cancel(accepted.run_id, **OWNER)
    else:
        current = service.execution.get(accepted.run_id)["server_run_id"]
        fake.runs[current]["interrupts"][0]["id"] = "replacement-native-id"
        new = await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
        assert new.pending_interactions[0]["revision"] == item["revision"] + 1
    row = service.interactions.repository.get_owned(
        item["interaction_id"], **OWNER, now=service.interactions.clock()
    )
    assert row["status"] == close
    with pytest.raises(InteractionConflict):
        await service.interactions.respond(
            item["interaction_id"], reply(item), scopes=SCOPES, idempotency_key="stale", **OWNER
        )
    assert len(fake.resume_calls) == 1


@pytest.mark.asyncio
async def test_accepted_answer_survives_crash_before_claim(tmp_path, monkeypatch):
    """决定与 prepared 操作必须同事务；带当前授权的查询可在重启后继续。"""
    _, fake, _, service, accepted, item = await parent_approval(tmp_path)
    original = service.interactions.operations.submit_prepared

    async def crash(_operation):
        """模拟回答提交事务之后、CAS 领取之前进程中断。"""
        raise RuntimeError("crash before claim")

    monkeypatch.setattr(service.interactions.operations, "submit_prepared", crash)
    with pytest.raises(RuntimeError, match="before claim"):
        await service.interactions.respond(
            item["interaction_id"], reply(item), scopes=SCOPES, idempotency_key="crash", **OWNER
        )
    row = service.interactions.repository.get_owned(
        item["interaction_id"], **OWNER, now=service.interactions.clock()
    )
    assert row["status"] == "resolved"
    assert service.execution.operation(row["operation_id"])["status"] == "prepared"
    monkeypatch.setattr(service.interactions.operations, "submit_prepared", original)
    service.interactions = InteractionService(
        fake, service.execution, agent_profiles=service.agent_profiles
    )
    assert (await service.status(accepted.run_id, scopes=SCOPES, **OWNER)).status == "completed"
    assert len(fake.resume_calls) == 2


@pytest.mark.asyncio
async def test_conflicting_second_answer_cannot_override_first(tmp_path):
    """决定不是可编辑草稿，换幂等键或换内容都不能撤回已经受理的授权。"""
    _, fake, _, service, _, item = await parent_approval(tmp_path)
    await service.interactions.respond(
        item["interaction_id"], reply(item), scopes=SCOPES, idempotency_key="first", **OWNER
    )
    for key, response in (
        ("second", reply(item)),
        ("first", reply(item).model_copy(update={"decision": "reject"})),
    ):
        with pytest.raises(InteractionConflict):
            await service.interactions.respond(
                item["interaction_id"], response, scopes=SCOPES, idempotency_key=key, **OWNER
            )
    assert len(fake.resume_calls) == 2


def question(identifier, point, kind):
    """产生框架同形状的声明式中断，不包含模型定义的权限或 Schema。"""
    return {
        "id": identifier,
        "value": {
            "kind": "user_interaction",
            "schema_version": 1,
            "point_id": point,
            "interaction_kind": kind,
            "question": "请确认本次研究范围。",
        },
    }


class QuestionsClient(FakeDelegationClient):
    """子任务先提槽再选择，只有子终态才恢复父 Agent。"""

    async def create_run(self, **kwargs):
        """初次子执行挂起在资料问题。"""
        server = await super().create_run(**kwargs)
        if kwargs["context"].get("parent_run_id"):
            self.runs[server.run_id].update(
                status="interrupted", interrupts=[question("input-1", "research_scope", "input")]
            )
        return server

    async def resume_run(self, **kwargs):
        """检查点原位恢复后继续到下一问题，不创建第二个子业务 run。"""
        if kwargs["context"].get("parent_run_id"):
            self.resume_calls.append(kwargs)
            value = next(iter(kwargs["command"]["resume"].values()))
            if value["kind"] == "input":
                assert value["answer"] == {"analysis_period": "最近一个月"}
                return {"__interrupt__": [question("choice-2", "research_focus", "choice")]}
            return {
                "structured_response": {
                    "outcome": "partial",
                    "summary": "用户选择风险与限制",
                    "limitations": ["test facts only"],
                }
            }
        return await super().resume_run(**kwargs)


@pytest.mark.asyncio
async def test_child_input_then_choice_resume_same_child_and_keep_parent_waiting(tmp_path):
    """C01：连续问题有新实例，输入不追加根 Journal，直到子终态才交付父。"""
    components, fake, _, service = stack(tmp_path, QuestionsClient())
    accepted, waiting, child = await started_child(service, fake, scopes=SCOPES)
    first = waiting.pending_interactions[0]
    assert waiting.waiting_reason == "input_required" and first["owner_run_id"] != accepted.run_id
    with pytest.raises(InteractionConflict):
        await service.interactions.respond(
            first["interaction_id"],
            reply(first, answer={"wrong": True}),
            scopes=SCOPES,
            idempotency_key="wrong",
            **OWNER,
        )
    await service.interactions.respond(
        first["interaction_id"],
        reply(first, answer={"analysis_period": "最近一个月"}),
        scopes=SCOPES,
        idempotency_key="input",
        **OWNER,
    )
    second_status = await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
    second = second_status.pending_interactions[0]
    assert second_status.waiting_reason == "choice_required"
    assert (
        second["interaction_id"] != first["interaction_id"]
        and second["owner_run_id"] == first["owner_run_id"]
    )
    assert len(components.conversation_repository.list_messages(accepted.conversation_id)) == 1
    assert len(fake.create_calls) == 2 and len(fake.resume_calls) == 1
    await service.interactions.respond(
        second["interaction_id"],
        reply(second, answer="风险与限制"),
        scopes=SCOPES,
        idempotency_key="choice",
        **OWNER,
    )
    final = await service.status(accepted.run_id, scopes=SCOPES, **OWNER)
    assert final.status == "completed" and len(fake.resume_calls) == 3
    assert (
        fake.resume_calls[0]["thread_id"] == fake.resume_calls[1]["thread_id"] == child["thread_id"]
    )


@pytest.mark.asyncio
async def test_http_interaction_endpoint_is_typed_owned_and_idempotent(tmp_path):
    """HTTP 新入口、旧 resume 共用决定，未知字段和跨主体请求不能提交。"""
    components, fake, delegation, service, accepted, item = await parent_approval(tmp_path)
    resolver = TargetResolver(
        tool_catalog=components.tool_catalog,
        agent_profiles=components.agent_profiles,
        workflow_catalog=components.workflow_catalog,
    )
    auth = StaticBearerAuthenticator(
        {
            "owner": AuthenticatedPrincipal(**OWNER, scopes=SCOPES),
            "other": AuthenticatedPrincipal(tenant_id="other", subject_id="other", scopes=SCOPES),
        }
    )
    app = create_app(
        run_service=RunService(fake, resolver),
        authenticator=auth,
        conversation_service=service,
        workflow_service=delegation.workflow_service,
        delegation_service=delegation,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer owner"},
    ) as client:
        url = f"/v1/interactions/{item['interaction_id']}"
        assert (await client.get(url, headers={"Authorization": "Bearer other"})).status_code == 404
        invalid = await client.post(
            url + "/responses",
            json={**reply(item).model_dump(), "edit": {}},
            headers={"Idempotency-Key": "http"},
        )
        assert invalid.status_code == 422
        first = await client.post(
            url + "/responses", json=reply(item).model_dump(), headers={"Idempotency-Key": "http"}
        )
        assert first.status_code == 202 and first.json()["status"] == "resolved"
        repeated = await client.post(
            url + "/responses", json=reply(item).model_dump(), headers={"Idempotency-Key": "http"}
        )
        assert repeated.status_code == 202 and len(fake.resume_calls) == 2
        assert (await client.get(f"/v1/runs/{accepted.run_id}")).json()["status"] == "completed"
