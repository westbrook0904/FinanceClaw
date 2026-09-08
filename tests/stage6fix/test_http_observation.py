"""A 阶段 HTTP/SSE 可见性、取消归属与不支持交互的安全投影。"""

import asyncio

import httpx
import pytest

from financeclaw.bff.http.app import create_app
from financeclaw.bff.http.auth import AuthenticatedPrincipal, StaticBearerAuthenticator
from financeclaw.coordination.application.run_observation import observe_run
from financeclaw.coordination.application.run_service import RunService
from financeclaw.coordination.application.target_resolver import TargetResolver
from tests.stage4.test_delegation import SCOPES
from tests.stage6fix.test_execution_recovery import (
    OWNER,
    ParentApprovalClient,
    stack,
    started_child,
)


def test_unknown_or_multiple_interrupts_are_never_completed():
    """未知中断、多 handoff 和多动作都必须可见等待，不能只处理第一项。"""
    for value in (
        {"status": "interrupted"},
        {"__interrupt__": [{"value": {"question": "?"}}]},
        {"interrupts": [{"value": {}}, {"value": {}}]},
        {
            "interrupts": [
                {
                    "value": {
                        "action_requests": [{"name": "x", "args": {}}, {"name": "y", "args": {}}],
                        "review_configs": [{}, {}],
                    }
                }
            ]
        },
    ):
        assert observe_run(value).kind == "unsupported"


def test_public_completion_excludes_checkpoint_and_reasoning_blocks():
    """对外完成结果与 Journal 使用同一文本提取，不返回整个执行状态。"""
    from financeclaw.coordination.application.conversation_runs import _public_output

    output = _public_output(
        {
            "private_state": {"secret": "not public"},
            "messages": [
                {"type": "tool", "content": "private tool input"},
                {
                    "type": "ai",
                    "content": [
                        {"type": "reasoning", "text": "private reasoning"},
                        {"type": "text", "text": "visible answer"},
                    ],
                },
            ],
        }
    )
    assert output == {"messages": [{"type": "assistant", "content": "visible answer"}]}


@pytest.mark.asyncio
async def test_http_sse_approval_and_cancel_are_owner_scoped(tmp_path):
    """A01/A11：审批有安全投影，等候期禁止另开 Turn，跨主体不能批准或取消。"""
    components, fake, delegation, service = stack(tmp_path, ParentApprovalClient())
    scopes = SCOPES | {"watchlist:write"}
    accepted, _, child = await started_child(service, fake, scopes=scopes)
    child["status"] = "success"
    resolver = TargetResolver(
        tool_catalog=components.tool_catalog,
        agent_profiles=components.agent_profiles,
        workflow_catalog=components.workflow_catalog,
    )
    auth = StaticBearerAuthenticator(
        {
            "owner": AuthenticatedPrincipal(**OWNER, scopes=scopes),
            "other": AuthenticatedPrincipal(tenant_id="other", subject_id="other", scopes=scopes),
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
        responses = await asyncio.gather(
            *(client.get(f"/v1/runs/{accepted.run_id}") for _ in range(6))
        )
        assert all(item.status_code == 200 for item in responses)
        waiting = (await client.get(f"/v1/runs/{accepted.run_id}")).json()
        assert (
            waiting["status"] == "interrupted" and waiting["waiting_reason"] == "approval_required"
        )
        assert len(fake.resume_calls) == 1
        streamed = await client.get(f"/v1/runs/{accepted.run_id}/events")
        assert "run.interrupted" in streamed.text and "approval_required" in streamed.text
        assert "assistant.completed" not in streamed.text
        conflict = await client.post(
            f"/v1/conversations/{accepted.conversation_id}/turns",
            json={"message": "new task"},
            headers={"Idempotency-Key": "new"},
        )
        assert conflict.status_code == 409
        denied = await client.post(
            f"/v1/runs/{accepted.run_id}/cancel", headers={"Authorization": "Bearer other"}
        )
        assert denied.status_code == 404
        malformed = await client.post(
            f"/v1/runs/{accepted.run_id}/resume",
            json={"type": "approve", "arguments_hash": "0" * 64},
        )
        assert malformed.status_code == 409 and len(fake.resume_calls) == 1
        cancelled = await client.post(f"/v1/runs/{accepted.run_id}/cancel")
        assert cancelled.json()["status"] == "cancelled"
        assert (await client.post(f"/v1/runs/{accepted.run_id}/cancel")).json()[
            "status"
        ] == "cancelled"
    components.database.close()
