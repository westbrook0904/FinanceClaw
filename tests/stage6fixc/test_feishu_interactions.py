"""C05：沿用已验证飞书文本事件，明确命令共用后端交互决定。"""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from financeclaw.application import FeishuChannelService
from financeclaw.application.feishu_interactions import parse_response
from financeclaw.modules.interactions import InteractionConflict
from tests.stage4.test_delegation import FakeDelegationClient
from tests.stage6.test_feishu_channel import _FakeGateway, _message
from tests.stage6fix.test_execution_recovery import stack
from tests.stage6fixc.test_interactions import SCOPES


class ChannelApprovalClient(FakeDelegationClient):
    """直接请求一个根写动作，底层执行仍使用独立恢复回执。"""

    async def create_run(self, **kwargs):
        """返回真实原生单动作审批形状。"""
        result = await super().create_run(**kwargs)
        self.runs[result.run_id].update(
            status="interrupted",
            interrupts=[
                {
                    "id": "channel-approval",
                    "value": {
                        "action_requests": [
                            {"name": "watchlist_add", "args": {"symbol": "AAPL", "note": "test"}}
                        ],
                        "review_configs": [
                            {
                                "action_name": "watchlist_add",
                                "allowed_decisions": ["approve", "reject"],
                            }
                        ],
                    },
                }
            ],
        )
        return result


async def waiting_channel(tmp_path, *, fallback=False):
    """走完整 P2P 身份映射与展示，而不是绕开 Channel 直接调用响应服务。"""
    components, fake, _, conversations = stack(tmp_path, ChannelApprovalClient())
    channel = FeishuChannelService(
        conversations,
        app_id="cli_test",
        allowed_open_ids=frozenset({"ou_a", "ou_b"}),
        scopes=SCOPES,
        status_poll_interval_seconds=0,
        status_timeout_seconds=2,
    )
    gateway = _FakeGateway(fail_after_stream=fallback)
    message = _message("first-message", "add AAPL")
    assert await channel.process(message, gateway) == "interrupted"
    context = fake.create_calls[0]["context"]
    owner = {"tenant_id": context["tenant_id"], "subject_id": context["subject_id"]}
    response = await conversations.status(context["run_id"], scopes=SCOPES, **owner)
    item = response.pending_interactions[0]
    return components, fake, conversations, channel, gateway, context, item


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.asyncio
async def test_explicit_approval_and_duplicate_message_do_not_create_turn(tmp_path, fallback):
    """普通同意不是授权；卡片失败也展示同一命令，重复消息不重做动作。"""
    components, fake, _, channel, gateway, context, item = await waiting_channel(
        tmp_path, fallback=fallback
    )
    shown = gateway.texts[-1]["text"] if fallback else gateway.streams[-1][2].content
    assert item["interaction_id"] in shown and f"/approve {item['interaction_id']}" in shown
    assert await channel.process(_message("vague", "同意"), gateway) == "waiting_active_turn"
    assert not fake.resume_calls
    command = _message(
        "approval-message",
        f"/approve {item['interaction_id']} {item['revision']} {item['action_hash']}",
    )
    assert await channel.process(command, gateway) == "completed"
    assert await channel.process(command, gateway) == "completed"
    assert len(fake.resume_calls) == 1 and len(fake.create_calls) == 1
    assert len(components.conversation_repository.list_messages(context["conversation_id"])) == 2


@pytest.mark.parametrize("fault", ["chat", "subject", "expired", "hash", "cancelled"])
@pytest.mark.asyncio
async def test_stale_or_foreign_channel_command_never_resumes(tmp_path, fault):
    """同用户不同单聊也不得跨会话审批；旧问题和修改摘要必须明确拒绝。"""
    _, fake, conversations, channel, gateway, context, item = await waiting_channel(tmp_path)
    message = _message(
        "attempt", f"/approve {item['interaction_id']} {item['revision']} {item['action_hash']}"
    )
    if fault == "chat":
        message = replace(message, chat_id="another-chat")
    if fault == "subject":
        message = replace(message, sender_open_id="ou_b")
    if fault == "hash":
        message = replace(
            message, text=f"/approve {item['interaction_id']} {item['revision']} {'0' * 64}"
        )
    if fault == "expired":
        conversations._clock = lambda: (
            datetime.fromisoformat(item["expires_at"]) + timedelta(seconds=1)
        )
    if fault == "cancelled":
        assert (
            await channel.process(_message("cancel", f"/cancel {context['run_id']}"), gateway)
            == "cancelled"
        )
    assert await channel.process(message, gateway) == "interaction_conflict"
    assert not fake.resume_calls


def test_question_command_preserves_json_and_never_infers_approval():
    """资料对象、字符串选择和授权类型独立，不解析自由文本为布尔批准。"""
    identifier, value = parse_response('/answer interaction-x 2 {"analysis_period":"最近一个月"}')
    assert identifier == "interaction-x" and value.answer == {"analysis_period": "最近一个月"}
    assert parse_response('/choose interaction-x 3 "风险与限制"')[1].answer == "风险与限制"
    assert parse_response("同意") is None
    with pytest.raises(InteractionConflict):
        parse_response("/approve interaction-x 1 同意")


def test_long_action_is_not_presented_as_a_complete_approval():
    """无法完整呈现动作时只给定位 API 和取消入口，不把截断摘要当成完整确认。"""
    from financeclaw.application.feishu_interactions import format_interactions

    message = format_interactions(
        (
            {
                "interaction_id": "interaction-long",
                "revision": 1,
                "question": "确认动作",
                "expires_at": "2026-09-07T00:00:00Z",
                "status": "pending",
                "kind": "approval",
                "action": {"details": "x" * 5000},
                "action_hash": "a" * 64,
                "allowed_decisions": ["approve", "reject"],
                "root_run_id": "root",
                "response_url": "/v1/interactions/interaction-long/responses",
            },
        ),
        fallback="pending",
    )
    assert "/approve " not in message and "未完整展示" in message
    assert "GET /v1/interactions/interaction-long" in message
