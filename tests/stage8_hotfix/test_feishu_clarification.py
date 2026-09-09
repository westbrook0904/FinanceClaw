"""飞书自然回复、原任务恢复与跨消息重推的持久化边界。"""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from financeclaw.bff.application.conversation_service import ConversationService
from financeclaw.bff.application.feishu_channel_service import (
    FeishuChannelService,
    FeishuInboundMessage,
    _ReplyState,
)
from financeclaw.bff.application.feishu_interactions import format_interactions, parse_response
from financeclaw.bff.notifications.rendering import render
from financeclaw.kernel.responses import StreamEvent
from financeclaw.shared.conversation.tables import ConversationTurnRow
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.root_repository import now
from financeclaw.shared.execution_ledger.run_tables import RootRunRow
from financeclaw.shared.releases.interactions import ROOT_CLARIFICATION
from tests.stage8_hotfix.test_bff_runs import SCOPES, final_state, tick
from tests.stage8_hotfix.test_bff_runs import runtime as runtime

OWNER = {"tenant_id": "feishu:tenant", "subject_id": "feishu:user"}
QUESTION = "请补充出生记录所用时制（当地钟表时间或真太阳时）。"


class Replies:
    """保留前台错误与提示；正常回答由现有持久通知流程交付。"""

    def __init__(self):
        """每个测试单独收集用户实际看到的文本。"""
        self.texts = []

    async def stream_markdown(self, **kwargs):
        """启用后台通知时不应生成另一份前台卡片。"""
        raise AssertionError("unexpected foreground stream")

    async def send_text(self, *, text, **kwargs):
        """收集提示并模拟已成功发送。"""
        self.texts.append(text)
        return True

    async def set_content(self, text):
        """模拟前台卡片用最终问题更新正文。"""
        self.texts.append(text)


def channel(runtime):
    """从数据库资源重建飞书入口，不保存当前问题到进程内存。"""
    runtime.runs.settings = runtime.runs.settings.model_copy(
        update={"feishu_notifications_enabled": True}
    )
    return FeishuChannelService(
        ConversationService(
            runtime.runs.repository, runtime.releases.agent_profiles, runs=runtime.runs
        ),
        app_id="app",
        allowed_open_ids=frozenset({"user"}),
        scopes=SCOPES,
    )


def message(text="请排盘", *, identifier="original", chat="chat"):
    """使用合成飞书身份与消息 ID，不访问真实渠道。"""
    return FeishuInboundMessage(
        message_id=identifier,
        tenant_key="tenant",
        sender_open_id="user",
        chat_id=chat,
        chat_type="p2p",
        content_type="text",
        text=text,
        sender_type="user",
    )


async def ask(runtime, accepted, *, native_id="clarification-1", approval=False):
    """按真实发布契约登记原生澄清或 HITL，生命周期负责生成待回答事实。"""
    state = final_state(runtime, accepted)
    if approval:
        call = {
            "id": native_id,
            "name": "watchlist_add",
            "args": {"symbol": "AAPL", "note": "synthetic"},
        }
        payload = {
            "action_requests": [{"name": call["name"], "args": call["args"]}],
            "review_configs": [
                {"action_name": call["name"], "allowed_decisions": ["approve", "reject"]}
            ],
        }
    else:
        call = {
            "id": native_id,
            "name": "request_user__clarification",
            "args": {"question": QUESTION},
        }
        payload = {
            "kind": "user_interaction",
            "schema_version": 1,
            "point_id": "clarification",
            "interaction_kind": "input",
            "question": QUESTION,
        }
    state["values"]["messages"][-1] = {"type": "ai", "content": "", "tool_calls": [call]}
    state["tasks"] = [{"interrupts": [{"id": native_id, "value": payload}]}]
    state["next"] = ["tools"]
    await tick(runtime)
    status = await runtime.runs.status(accepted.run_id, **OWNER)
    assert status.status == "interrupted"
    return status.pending_interactions[0]


async def waiting(runtime, *, approval=False):
    """实际经过飞书受理、持久根提交及后台中断观察。"""
    service, replies = channel(runtime), Replies()
    assert await service.process(message(), replies) == "accepted"
    with runtime.runs.store.sessions() as session:
        root = session.scalar(select(RootRunRow))
        accepted = SimpleNamespace(run_id=root.run_id, thread_id=root.projection["thread_id"])
    await tick(runtime)
    item = await ask(runtime, accepted, approval=approval)
    return service, replies, accepted, item


@pytest.mark.asyncio
async def test_plain_answer_resumes_same_root_and_replay_never_answers_next_question(runtime):
    """只回复时制即可恢复；重启入口及重推原请求／旧回答不会串到后续澄清。"""
    service, replies, accepted, first = await waiting(runtime)
    text = render("interaction", {"run_id": accepted.run_id, "interactions": [first]})
    assert QUESTION in text and "直接回复" in text
    assert all(part not in text for part in ("interaction-", "Schema", '"properties"', "/v1/"))
    # 原消息重推仍展示同一根，不得拿原始任务当作澄清回答。
    assert await channel(runtime).process(message(), replies) == "accepted"
    answer = message("当地钟表时间", identifier="answer-1")
    assert await service.process(answer, replies) == "accepted"
    with runtime.runs.store.sessions() as session:
        saved = session.get(PendingInteractionRow, first["interaction_id"])
        assert saved.response["answer"] == {"text": "当地钟表时间"}
        operation_id = saved.operation_id
    await tick(runtime)
    assert len(runtime.client.runs.calls) == 2
    second = await ask(runtime, accepted, native_id="clarification-2")
    assert second["revision"] == first["revision"] + 1
    service = channel(runtime)
    assert await service.process(answer, replies) == "accepted"
    assert await service.process(message(), replies) == "accepted"
    with runtime.runs.store.sessions() as session:
        assert (
            session.get(PendingInteractionRow, first["interaction_id"]).operation_id == operation_id
        )
        assert session.get(PendingInteractionRow, second["interaction_id"]).response is None
        assert session.scalar(select(func.count()).select_from(ConversationTurnRow)) == 1
    assert await service.process(message("公历", identifier="answer-2"), replies) == "accepted"
    await tick(runtime)
    final_state(runtime, accepted)
    await tick(runtime)
    assert (await runtime.runs.status(accepted.run_id, **OWNER)).status == "completed"
    assert len(runtime.client.runs.calls) == 3
    assert await channel(runtime).process(answer, replies) == "accepted"
    assert not replies.texts


@pytest.mark.asyncio
async def test_stream_poll_and_durable_notification_show_same_concise_question(runtime):
    """SSE、轮询和独立通知发送器均展示问题与直接回复说明。"""
    service, replies, accepted, item = await waiting(runtime)
    expected = render("interaction", {"run_id": accepted.run_id, "interactions": [item]})
    await service._apply_event(
        replies,
        StreamEvent(event="run.interrupted", data={"pending_interactions": [item]}),
        _ReplyState(),
    )
    await service._resolve_final(replies, _ReplyState(), run_id=accepted.run_id, **OWNER)
    assert replies.texts == [expected, expected]


@pytest.mark.asyncio
async def test_plain_agreement_cannot_approve_but_explicit_approval_still_works(runtime):
    """审批显示明确动作入口，普通“同意”不提交决定；既有带摘要命令仍然可用。"""
    service, replies, accepted, item = await waiting(runtime, approval=True)
    assert (
        await service.process(message("同意", identifier="agree"), replies) == "waiting_active_turn"
    )
    assert "/approve" in replies.texts[-1] and "直接回复" not in replies.texts[-1]
    with runtime.runs.store.sessions() as session:
        assert session.get(PendingInteractionRow, item["interaction_id"]).response is None
    command = f"/approve {item['interaction_id']} {item['revision']} {item['action_hash']}"
    assert await service.process(message(command, identifier="approve"), replies) == "accepted"


@pytest.mark.asyncio
async def test_expired_question_has_actionable_reply_and_cancel(runtime):
    """已过期的回答不变成新任务，提示提供绑定该根的取消入口。"""
    service, replies, accepted, item = await waiting(runtime)
    with runtime.runs.store.sessions.begin() as session:
        session.get(PendingInteractionRow, item["interaction_id"]).expires_at = now() - timedelta(
            seconds=1
        )
    result = await service.process(message("当地钟表时间", identifier="late"), replies)
    assert result == "waiting_active_turn" and "过期" in replies.texts[-1]
    assert "Web/API" not in replies.texts[-1] and "/cancel" in replies.texts[-1]
    cancel = message(f"/cancel {accepted.run_id}", identifier="cancel")
    result = await service.process(cancel, replies)
    assert result in {"cancellation_requested", "cancelled"}
    await tick(runtime)
    assert (await runtime.runs.status(accepted.run_id, **OWNER)).status == "cancelled"
    assert await service.process(cancel, replies) == "cancelled"


@pytest.mark.asyncio
async def test_other_chat_and_changed_duplicate_cannot_retarget_answer(runtime):
    """回复绑定当前单聊与原消息内容，不能跨会话寻找另一个人的待回答事项。"""
    service, replies, accepted, item = await waiting(runtime)
    answer = message("当地钟表时间", identifier="answer")
    assert await service.process(replace(answer, chat_id="another-chat"), replies) == "accepted"
    with runtime.runs.store.sessions() as session:
        assert session.get(PendingInteractionRow, item["interaction_id"]).response is None
    answer = replace(answer, message_id="answer-in-original-chat")
    assert await service.process(answer, replies) == "accepted"
    assert (
        await service.process(replace(answer, text="真太阳时"), replies) == "interaction_conflict"
    )
    with runtime.runs.store.sessions() as session:
        assert session.get(PendingInteractionRow, item["interaction_id"]).response["answer"] == {
            "text": "当地钟表时间"
        }


@pytest.mark.asyncio
async def test_answer_schema_and_legacy_answer_command_are_preserved(runtime):
    """文本捷径仍使用固定回答 Schema；原来的显式回答命令也能恢复同一任务。"""
    service, replies, accepted, item = await waiting(runtime)
    assert (
        await service.process(message("a" * 8001, identifier="long"), replies)
        == "interaction_conflict"
    )
    command = f'/answer {item["interaction_id"]} {item["revision"]} {{"text":"当地钟表时间"}}'
    assert await service.process(message(command, identifier="explicit"), replies) == "accepted"
    with runtime.runs.store.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ConversationTurnRow)) == 1


@pytest.mark.asyncio
async def test_replayed_message_cannot_answer_new_question_when_lookup_races(runtime, monkeypatch):
    """提交时再次验证消息绑定，即使渠道查找与后台推进交错也不复用旧答案。"""
    service, replies, accepted, first = await waiting(runtime)
    answer = message("当地钟表时间", identifier="race-answer")
    assert await service.process(answer, replies) == "accepted"
    await tick(runtime)
    second = await ask(runtime, accepted, native_id="racing-question")

    def stale_lookup(*args, **kwargs):
        """模拟查询重放记录后、读取当前问题前，另一进程完成首次回答。"""
        return {"root_run_id": accepted.run_id, "interactions": [second]}

    monkeypatch.setattr(runtime.runs.interactions, "channel_state", stale_lookup)
    assert await service.process(answer, replies) == "interaction_conflict"
    assert await service.process(message(), replies) == "interaction_conflict"
    with runtime.runs.store.sessions() as session:
        assert session.get(PendingInteractionRow, second["interaction_id"]).response is None


def test_multiple_questions_show_valid_explicit_commands_instead_of_ambiguous_plain_reply():
    """多问题不得宣称支持不带目标的回答；示例 JSON 符合 text 契约。"""
    item = {
        "interaction_id": "interaction-one",
        "revision": 1,
        "kind": "input",
        "status": "pending",
        "question": QUESTION,
        "response_schema": ROOT_CLARIFICATION.response_schema,
        "expires_at": (now() + timedelta(minutes=15)).isoformat(),
        "root_run_id": "synthetic-run",
        "response_url": "/v1/interactions/interaction-one/responses",
    }
    rendered = format_interactions(
        [item, {**item, "interaction_id": "interaction-two"}], fallback="等待"
    )
    assert "直接回复" not in rendered and "按上述格式填写" not in rendered
    commands = [line for line in rendered.splitlines() if line.startswith("/answer")]
    assert len(commands) == 2
    for command in commands:
        _, response = parse_response(command)
        assert response.answer == {"text": "你的回答"}
