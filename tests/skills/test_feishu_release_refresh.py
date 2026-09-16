"""飞书旧会话发布更新：复现 /skills LookupError，并保护历史与活动任务。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import MetaData, func, select

from financeclaw.kernel.agents import AgentProfileCatalog
from financeclaw.shared.conversation.tables import (
    ChannelConversationBindingRow,
    ConversationMessageRow,
    ConversationRow,
)
from financeclaw.shared.infrastructure.orm import Base
from financeclaw.shared.notifications.facts import target_valid
from financeclaw.shared.notifications.tables import NotificationEventRow, NotificationTargetRow
from financeclaw.shared.turns.tables import ConversationTurnRow, InteractionRow
from tests.skills.test_feishu_forms import callback, drain
from tests.stage8.test_notifications import Gateway
from tests.stage8_hotfix.test_feishu_clarification import Replies, channel, message, waiting
from tests.stage10.runtime import final_state, tick
from tests.stage10.runtime import runtime as runtime


def old_catalog(runtime, patch):
    """模拟更新前真实发布，受理仍经过正式目录、事务和通知路径。"""
    profiles = runtime.turns.releases.agents
    old = AgentProfileCatalog(
        profile.model_copy(update={"version": "1.6.0"})
        if profile.agent_id == "finance_agent"
        else profile
        for profile in profiles.values()
    )
    patch.setattr(runtime.turns.releases, "agents", old)
    patch.setattr(runtime.releases, "agent_profiles", old)


def legacy_binding(runtime, *, version="1.6.0"):
    """创建没有任务的旧单聊绑定，复现数据库保留旧版本、容器只装新版本。"""
    return runtime.turns.journal.get_or_create_channel_conversation(
        channel="feishu",
        app_id="app",
        tenant_key="tenant",
        external_user_id="user",
        external_chat_id="chat",
        tenant_id="feishu:tenant",
        subject_id="feishu:user",
        agent_id="finance_agent",
        agent_profile_version=version,
    )[1]


@pytest.mark.asyncio
async def test_completed_old_release_opens_form_and_keeps_history_and_receipts(
    runtime, monkeypatch
):
    """已结束旧任务保持原快照和通知归属，表单提交的新任务使用新发布与新线程。"""
    with monkeypatch.context() as patch:
        old_catalog(runtime, patch)
        assert await channel(runtime).process(message("旧任务"), Replies()) == "accepted"
        with runtime.turns.sessions() as session:
            root = session.scalar(select(ConversationTurnRow))
            accepted = SimpleNamespace(turn_id=root.turn_id)
            old_snapshot, old_thread = root.release_snapshot, root.thread_id
            conversation_id = root.conversation_id
        await tick(runtime)
        final_state(runtime, accepted, content="原任务已完成")
        await tick(runtime)
    with runtime.turns.sessions() as session:
        assert session.get(ConversationTurnRow, accepted.turn_id).status == "completed"
        original_messages = list(session.scalars(select(ConversationMessageRow.content)))
        binding_id = session.scalar(select(ChannelConversationBindingRow.binding_id))
    service, replies = channel(runtime), Replies()
    assert await service.process(message("/skills", identifier="new-form"), replies) == "skill_form"
    assert not replies.texts
    with runtime.turns.sessions() as session:
        conversation = session.get(ConversationRow, conversation_id)
        new_thread = conversation.agent_thread_id
        assert conversation.agent_profile_version == "1.8.0" and new_thread != old_thread
        assert session.scalar(select(func.count()).select_from(ConversationRow)) == 1
        assert session.scalar(select(ChannelConversationBindingRow.binding_id)) == binding_id
        assert list(session.scalars(select(ConversationMessageRow.content))) == original_messages
        assert session.get(ConversationTurnRow, accepted.turn_id).release_snapshot == old_snapshot
        old_target = session.scalar(
            select(NotificationTargetRow).where(NotificationTargetRow.turn_id == accepted.turn_id)
        )
        assert target_valid(session, old_target)
        assert session.scalar(select(func.count()).select_from(ConversationTurnRow)) == 1
    gateway = Gateway()
    await drain(runtime, gateway)
    with runtime.turns.sessions() as session:
        view = session.scalar(
            select(NotificationEventRow).where(NotificationEventRow.kind == "skill_form")
        )
        target = session.get(NotificationTargetRow, view.target_id)
    raw = callback(view)
    raw["event"]["context"]["open_message_id"] = target.card_message_id
    assert (await service.card_actions.handle(raw))["toast"]["type"] == "info"
    with runtime.turns.sessions() as session:
        new_root = session.scalar(
            select(ConversationTurnRow).where(ConversationTurnRow.turn_id != accepted.turn_id)
        )
        assert new_root.thread_id == new_thread
        assert new_root.release_snapshot["profile"]["version"] == "1.8.0"
        assert session.get(ConversationTurnRow, accepted.turn_id).thread_id == old_thread
    await tick(runtime)
    assert runtime.client.calls[-1]["thread_id"] == new_thread


@pytest.mark.asyncio
async def test_active_old_release_has_clear_hint_and_can_stop_before_refresh(runtime, monkeypatch):
    """待答旧任务不被升级或回答；仍可停止，确认停止后再打开新发布表单。"""
    with monkeypatch.context() as patch:
        old_catalog(runtime, patch)
        _, _, accepted, item = await waiting(runtime)
    service, replies = channel(runtime), Replies()
    assert (
        await service.process(message("/skills", identifier="new-form"), replies)
        == "skill_form_unavailable"
    )
    assert "先停止" in replies.texts[-1] and "处理失败" not in replies.texts[-1]
    with runtime.turns.sessions() as session:
        root = session.get(ConversationTurnRow, accepted.turn_id)
        conversation = session.get(ConversationRow, root.conversation_id)
        assert conversation.agent_profile_version == "1.6.0"
        assert conversation.agent_thread_id == root.thread_id
        assert session.get(InteractionRow, item["interaction_id"]).response is None
        assert (
            session.scalar(
                select(NotificationEventRow).where(NotificationEventRow.kind == "skill_form")
            )
            is None
        )
    assert await service.process(
        message(f"/cancel {accepted.turn_id}", identifier="stop-old"), replies
    ) in {"cancelling", "cancelled"}
    await tick(runtime)
    await tick(runtime)
    with runtime.turns.sessions() as session:
        assert session.get(ConversationTurnRow, accepted.turn_id).status == "cancelled"
    assert (
        await service.process(message("/skills", identifier="after-stop"), replies) == "skill_form"
    )


@pytest.mark.asyncio
async def test_concurrent_form_open_refreshes_native_thread_only_once(runtime, monkeypatch):
    """不同 API 实例并发打开旧单聊时，第二个事务重新读取版本，不重复换线程。"""
    import financeclaw.shared.conversation.repository as repository

    old = legacy_binding(runtime)
    original_uuid = repository.uuid4
    generated = []

    def counted_uuid():
        """记录会话仓库创建的线程标识，不干预其他模块的任务或通知标识。"""
        value = original_uuid()
        generated.append(str(value))
        return value

    monkeypatch.setattr(repository, "uuid4", counted_uuid)
    results = await asyncio.gather(
        channel(runtime).process(message("/skills", identifier="form-a"), Replies()),
        channel(runtime).process(message("/skills", identifier="form-b"), Replies()),
    )
    assert results == ["skill_form", "skill_form"]
    with runtime.turns.sessions() as session:
        conversation = session.get(ConversationRow, old.conversation_id)
        assert generated == [conversation.agent_thread_id]
        assert conversation.agent_thread_id != old.agent_thread_id
        assert session.scalar(select(func.count()).select_from(ConversationTurnRow)) == 0
    assert not runtime.client.calls


@pytest.mark.asyncio
async def test_older_process_cannot_downgrade_newer_conversation(runtime):
    """滚动部署中的旧进程看到更新的会话时，只给版本提示，不改版本或线程。"""
    old = legacy_binding(runtime, version="1.9.0")
    replies = Replies()
    assert await channel(runtime).process(message("/skills"), replies) == "skill_form_unavailable"
    assert "版本不一致" in replies.texts[-1]
    with runtime.turns.sessions() as session:
        conversation = session.get(ConversationRow, old.conversation_id)
        assert conversation.agent_profile_version == "1.9.0"
        assert conversation.agent_thread_id == old.agent_thread_id
        assert session.scalar(select(func.count()).select_from(NotificationTargetRow)) == 0


@pytest.mark.asyncio
async def test_identity_mismatch_cannot_refresh_another_users_release(runtime):
    """身份验证先于版本更新，白名单中的另一用户也不能改变已绑定会话。"""
    old = legacy_binding(runtime)
    service = channel(runtime)
    service.allowed_open_ids = frozenset({"user", "other"})
    await service.process(replace(message("/skills"), sender_open_id="other"), Replies())
    with runtime.turns.sessions() as session:
        conversation = session.get(ConversationRow, old.conversation_id)
        assert conversation.agent_profile_version == "1.6.0"
        assert conversation.agent_thread_id == old.agent_thread_id
        assert session.scalar(select(func.count()).select_from(NotificationTargetRow)) == 0


@pytest.mark.asyncio
async def test_old_unsubmitted_form_cannot_submit_after_release_refresh(runtime, monkeypatch):
    """版本更新保留通知归属，但旧表单冻结的发布必须重新选择，不能静默升级任务。"""
    with monkeypatch.context() as patch:
        old_catalog(runtime, patch)
        assert await channel(runtime).process(message("/skills"), Replies()) == "skill_form"
    await drain(runtime, Gateway())
    with runtime.turns.sessions() as session:
        view = session.scalar(select(NotificationEventRow))
    service = channel(runtime)
    assert (
        await service.process(message("/skills", identifier="new-form"), Replies()) == "skill_form"
    )
    result = await service.card_actions.handle(callback(view))
    assert result["toast"]["type"] == "error" and "已更新" in result["toast"]["content"]
    with runtime.turns.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ConversationTurnRow)) == 0


@pytest.mark.asyncio
async def test_api_readiness_rejects_old_form_schema(runtime, monkeypatch):
    """旧库表齐全但草稿关联列非空时返回 503；补齐结构后恢复就绪。"""
    from financeclaw.api.bootstrap import create_default_app

    settings = runtime.turns.settings.model_copy(update={"feishu_enabled": True})
    app = create_default_app(settings, client=runtime.client)
    app.state.resources = runtime.resources
    app.state.turns = runtime.turns
    monkeypatch.setattr(runtime.turns.lifecycle, "healthy", AsyncMock(return_value=True))
    monkeypatch.setattr(runtime.turns.events, "healthy", AsyncMock(return_value=True))
    engine = runtime.resources.database.engine
    legacy = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(legacy)
    legacy.tables["notification_targets"].c.turn_id.nullable = False
    NotificationTargetRow.__table__.drop(engine)
    legacy.tables["notification_targets"].create(engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/health/ready")).status_code == 503
        legacy.tables["notification_targets"].drop(engine)
        NotificationTargetRow.__table__.create(engine)
        response = await client.get("/v1/health/ready")
        assert response.status_code == 200 and response.json() == {"ready": True}
