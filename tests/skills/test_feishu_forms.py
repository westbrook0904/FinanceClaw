"""技能表单从持久投递到原子受理的回归，外部飞书传输使用合成账本。"""

import asyncio
import json
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import func, select

from financeclaw.api.application.feishu_card_actions import button_values
from financeclaw.api.http.channels import channel_router
from financeclaw.integrations.notifications.repository import NotificationRepository
from financeclaw.integrations.notifications.worker import deliver
from financeclaw.shared.channels.feishu.skill_cards import render_skill_form
from financeclaw.shared.conversation.tables import ConversationMessageRow
from financeclaw.shared.notifications.tables import NotificationDeliveryRow as Delivery
from financeclaw.shared.notifications.tables import NotificationEventRow as Event
from financeclaw.shared.notifications.tables import NotificationTargetRow as Target
from financeclaw.shared.skills.catalog import SkillCatalog
from financeclaw.shared.turns.tables import ConversationTurnRow, InteractionRow, TurnCommandRow
from financeclaw.shared.turns.types import now
from tests.stage8.test_notifications import Gateway
from tests.stage8_hotfix.test_feishu_clarification import Replies, channel, message, waiting
from tests.stage10.runtime import runtime as runtime
from tests.stage10.runtime import tick

TASK = "我有金酒、柠檬和苏打水，没有摇壶，请给出两人份的清爽配方。"


def sender(runtime):
    """重建独立发送器，所有状态只从 SQLite 持久通知表读取。"""
    return NotificationRepository(
        runtime.turns.sessions, app_id="app", allowed_open_ids=frozenset({"user"})
    )


async def drain(runtime, gateway):
    """投递当前已冻结视图，沿用正式 sender 的有效性、租约与回执流程。"""
    repository = sender(runtime)
    while repository.materialize():
        pass
    for _ in range(20):
        claim = repository.claim("form-sender", lease_seconds=60)
        if claim is None:
            return
        await deliver(repository, gateway, claim, runtime.turns.settings)
    raise AssertionError("skill form sender did not drain")


async def opened(runtime, *, scopes=None, gateway=None):
    """发送 /skills 并取得已投递表单，测试不预先创建业务 Turn。"""
    service, replies = channel(runtime), Replies()
    if scopes is not None:
        service.scopes = scopes
        service.card_actions.scopes = scopes
    assert (
        await service.process(message("/skills", identifier="skills-open"), replies) == "skill_form"
    )
    assert not replies.texts
    gateway = gateway or Gateway()
    await drain(runtime, gateway)
    with runtime.turns.sessions() as session:
        view = session.scalar(select(Event).where(Event.kind == "skill_form"))
    return service, view, gateway


def callback(view, *, skill="cocktail-from-what-i-have", task=TASK, event_key="submit-1"):
    """按已送达选项生成真实 callback 结构，不以隐藏参数直接指定版本。"""
    option = next(
        f"o{index}"
        for index, item in enumerate(view.payload["skills"])
        if item["ref"]["skill_id"] == skill
    )
    return {
        "schema": "2.0",
        "header": {
            "app_id": "app",
            "tenant_key": "tenant",
            "event_type": "card.action.trigger",
            "event_id": event_key,
        },
        "event": {
            "operator": {"open_id": "user", "tenant_key": "tenant"},
            "context": {"open_chat_id": "chat", "open_message_id": "message-1"},
            "action": {
                "value": next(button_values(render_skill_form(view.event_id, view.payload))),
                "form_value": {"skill": option, "task_description": task},
            },
        },
    }


def assert_no_task(runtime):
    """表单展示或拒绝不能留下任务、执行命令或伪用户 Journal。"""
    with runtime.turns.sessions() as session:
        for row in (ConversationTurnRow, TurnCommandRow, ConversationMessageRow):
            assert session.scalar(select(func.count()).select_from(row)) == 0
    assert not runtime.client.calls


@pytest.mark.asyncio
async def test_open_form_is_durable_idempotent_and_does_not_start_a_task(runtime):
    """页面有中文选项、多行必填描述和一次提交按钮，重放入站消息不再发卡。"""
    service, view, gateway = await opened(runtime)
    card = json.loads(gateway.calls[0]["content"])
    assert card["header"]["title"]["content"] == "FinanceClaw · 新建技能任务"
    form = card["body"]["elements"][0]
    select_widget = next(item for item in form["elements"] if item["tag"] == "select_static")
    assert {item["text"]["content"] for item in select_widget["options"]} == {
        "行情简报",
        "现有材料调酒",
    }
    text_widget = next(item for item in form["elements"] if item["tag"] == "input")
    assert text_widget["input_type"] == "multiline_text"
    assert text_widget["max_length"] == 1000
    assert select_widget["required"] and text_widget["required"]
    assert not select_widget.get("behaviors")
    assert len(list(button_values(card))) == 1
    assert "选择仅对本次任务生效。" in gateway.calls[0]["content"]
    assert_no_task(runtime)
    assert (
        await channel(runtime).process(message("/skills", identifier="skills-open"), Replies())
        == "skill_form"
    )
    await drain(runtime, gateway)
    assert len(gateway.calls) == 1
    with runtime.turns.sessions() as session:
        target = session.get(Target, view.target_id)
        assert target.turn_id is None and target.card_message_id == "message-1"
        assert session.scalar(select(func.count()).select_from(Event)) == 1
    assert_no_task(runtime)


@pytest.mark.asyncio
async def test_submit_pins_skill_once_and_updates_original_card_after_restart(runtime):
    """提交前无执行；并发双击只创建一个任务，既有卡片原位显示受理回执。"""
    _, view, gateway = await opened(runtime, scopes=frozenset({"tools:read"}))
    first, second = callback(view), callback(view, event_key="submit-2")
    services = [channel(runtime), channel(runtime)]
    for service in services:
        service.card_actions.scopes = frozenset({"tools:read"})
    results = await asyncio.gather(
        services[0].card_actions.handle(first), services[1].card_actions.handle(second)
    )
    assert results[0] == results[1]
    assert "已受理 · 现有材料调酒" in results[0]["toast"]["content"]
    assert not runtime.client.calls
    with runtime.turns.sessions() as session:
        roots = session.scalars(select(ConversationTurnRow)).all()
        assert len(roots) == 1
        root = roots[0]
        assert root.status == "accepted" and set(root.grant_scopes) == {"tools:read"}
        selected = root.release_snapshot["requested_skills"]
        assert selected == [view.payload["skills"][0]["ref"]]
        command = session.scalar(select(TurnCommandRow))
        assert command.request_payload["input"]["messages"][0]["content"] == (
            "/skill cocktail-from-what-i-have " + TASK
        )
        target = session.get(Target, view.target_id)
        assert target.turn_id == root.turn_id and target.card_message_id == "message-1"
        assert root.turn_id in results[0]["toast"]["content"]
    await drain(runtime, gateway)
    assert gateway.calls[-1]["target_message_id"] == "message-1"
    assert "已受理 · 现有材料调酒" in gateway.calls[-1]["content"]
    assert "任务编号" in gateway.calls[-1]["content"]
    assert "skill_task_form" not in gateway.calls[-1]["content"]
    assert len({claim["card_id"] for claim in gateway.calls}) == 1
    assert await services[0].card_actions.handle(first) == results[0]
    changed = callback(view, task="不同任务", event_key="different")
    assert (await services[0].card_actions.handle(changed))["toast"]["type"] == "error"
    await tick(runtime)
    assert len(runtime.client.calls) == 1
    assert runtime.client.calls[0]["input"]["messages"][0]["content"].startswith(
        "/skill cocktail-from-what-i-have "
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["", "   ", "a" * 1001, ["文本"], True])
async def test_invalid_description_does_not_consume_form(runtime, task):
    """浏览器之外伪造的空白、超长及错误类型输入也由服务器拒绝。"""
    service, view, _ = await opened(runtime)
    result = await service.card_actions.handle(callback(view, task=task))
    assert result["toast"]["type"] == "error"
    assert_no_task(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tampering", ["app", "tenant", "user", "chat", "message", "value", "extra", "choice"]
)
async def test_foreign_callbacks_and_unpublished_fields_are_rejected(runtime, tampering):
    """原用户、原单聊、原消息和冻结按钮必须同时匹配，客户端不能扩大授权。"""
    service, view, _ = await opened(runtime)
    raw = callback(view)
    if tampering == "app":
        raw["header"]["app_id"] = "other"
    elif tampering in {"tenant", "user"}:
        raw["event"]["operator"]["tenant_key" if tampering == "tenant" else "open_id"] = "other"
        if tampering == "tenant":
            raw["header"]["tenant_key"] = "other"
        else:
            service.card_actions.allowed_open_ids = frozenset({"user", "other"})
    elif tampering in {"chat", "message"}:
        raw["event"]["context"][f"open_{tampering}_id"] = "other"
    elif tampering == "value":
        raw["event"]["action"]["value"]["scopes"] = ["*"]
    elif tampering == "extra":
        raw["event"]["action"]["form_value"]["package_hash"] = "a" * 64
    else:
        raw["event"]["action"]["form_value"]["skill"] = "not-published"
    assert (await service.card_actions.handle(raw))["toast"]["type"] == "error"
    assert_no_task(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "scope_revoked", "release_changed", "disabled"])
async def test_submission_rechecks_expiry_permissions_and_pinned_release(runtime, change):
    """表单快照不是持久授权，提交前的撤权、发布替换或过期都会生效。"""
    service, view, _ = await opened(runtime)
    raw = callback(view, skill="market-brief")
    if change == "expired":
        with runtime.turns.sessions.begin() as session:
            row = session.get(Event, view.event_id)
            row.payload = {**row.payload, "expires_at": (now() - timedelta(seconds=1)).isoformat()}
    elif change == "scope_revoked":
        service.card_actions.scopes = frozenset({"tools:read"})
    else:
        entries = []
        for release, package in runtime.turns.releases.skills.entries.values():
            if release.ref.skill_id == "market-brief":
                policy = {"enabled": False} if change == "disabled" else {"policy_version": "new"}
                release = release.model_copy(update=policy)
            entries.append((release, package))
        runtime.turns.releases.skills = SkillCatalog(entries)
    result = await service.card_actions.handle(raw)
    assert result["toast"]["type"] == "error" and "/skills" in result["toast"]["content"]
    assert_no_task(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["receipt", "deadline"])
async def test_commit_failure_rolls_back_task_target_and_receipt(runtime, monkeypatch, failure):
    """受理末尾异常或超过回调时限都整笔回滚，同一表单仍可再次提交。"""
    import financeclaw.api.application.feishu_card_actions as actions
    import financeclaw.api.application.skill_task_forms as forms

    service, view, _ = await opened(runtime)

    def crash(*args):
        """在创建任务并关联目标之后注入提交前异常。"""
        raise RuntimeError("synthetic receipt failure")

    raw = callback(view)
    with monkeypatch.context() as patch:
        if failure == "receipt":
            patch.setattr(forms, "save_receipt", crash)
        else:
            # 只替换该回调模块的时钟引用，保留 asyncio 与事务驱动的真实时钟。
            patch.setattr(actions, "time", SimpleNamespace(monotonic=iter([0, 2]).__next__))
        assert (await service.card_actions.handle(raw))["toast"]["type"] == "warning"
    assert_no_task(runtime)
    with runtime.turns.sessions() as session:
        assert session.get(Target, view.target_id).turn_id is None
        assert session.scalar(select(func.count()).select_from(Event)) == 1
    assert (await channel(runtime).card_actions.handle(raw))["toast"]["type"] == "info"


@pytest.mark.asyncio
async def test_lost_form_delivery_cannot_admit_unconfirmed_callback(runtime):
    """表单首发丢回执按既有 uncertain 规则保留原发送键，不能伪造送达确认。"""
    service, view, gateway = await opened(runtime, gateway=Gateway(lose=True))
    assert (await service.card_actions.handle(callback(view)))["toast"]["type"] == "warning"
    await drain(runtime, gateway)
    assert len(gateway.calls) == 1
    with runtime.turns.sessions() as session:
        delivery = session.scalar(select(Delivery))
        assert delivery.status == "uncertain"
        assert session.get(Target, view.target_id).card_message_id is None
    assert_no_task(runtime)


@pytest.mark.asyncio
async def test_opening_form_does_not_answer_or_interrupt_current_question(runtime):
    """/skills 是独立表单入口，既有澄清仍等待，提交新任务时明确提示先完成上一条。"""
    service, _, accepted, item = await waiting(runtime)
    assert (
        await service.process(message("/skills", identifier="skills-open"), Replies())
        == "skill_form"
    )
    gateway = Gateway()
    await drain(runtime, gateway)
    with runtime.turns.sessions() as session:
        view = session.scalar(select(Event).where(Event.kind == "skill_form"))
        target = session.get(Target, view.target_id)
        message_id = target.card_message_id
        assert session.get(InteractionRow, item["interaction_id"]).response is None
        assert session.scalar(select(func.count()).select_from(ConversationTurnRow)) == 1
    raw = callback(view)
    raw["event"]["context"]["open_message_id"] = message_id
    result = await service.card_actions.handle(raw)
    assert "上一条任务" in result["toast"]["content"]
    with runtime.turns.sessions() as session:
        assert session.get(Target, view.target_id).turn_id is None
        assert session.get(InteractionRow, item["interaction_id"]).response is None


@pytest.mark.asyncio
async def test_no_available_skills_returns_hint_without_draft_or_task(runtime):
    """关闭技能发布时不给出空下拉框，也不把 /skills 当普通任务发送给模型。"""
    runtime.turns.releases.skills = SkillCatalog()
    replies = Replies()
    assert await channel(runtime).process(message("/skills"), replies) == "skill_form_empty"
    assert replies.texts == ["当前没有可用技能。"]
    with runtime.turns.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Target)) == 0
    assert_no_task(runtime)


@pytest.mark.asyncio
async def test_form_and_submit_use_authenticated_internal_http_ingress(runtime):
    """渠道进程经既有 HTTP 入口打开表单和提交字段，无需新增卡片发送通道。"""
    settings = runtime.turns.settings.model_copy(
        update={"integration_service_token": SecretStr("synthetic-form-service-token-for-testing")}
    )
    app = FastAPI()
    app.include_router(channel_router(channel(runtime), settings))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/internal/channels/feishu/events"
        request = {"kind": "message", "message": asdict(message("/skills"))}
        assert (await client.post(path, json=request)).status_code == 401
        assert_no_task(runtime)
        client.headers["Authorization"] = "Bearer synthetic-form-service-token-for-testing"
        response = await client.post(path, json=request)
        assert response.status_code == 200
        assert response.json() == {"status": "skill_form", "replies": []}
        assert_no_task(runtime)
        await drain(runtime, Gateway())
        with runtime.turns.sessions() as session:
            view = session.scalar(select(Event).where(Event.kind == "skill_form"))
        response = await client.post(path, json={"kind": "card", "event": callback(view)})
        assert response.status_code == 200
        assert "已受理 · 现有材料调酒" in response.json()["toast"]["content"]
    with runtime.turns.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ConversationTurnRow)) == 1


@pytest.mark.asyncio
async def test_form_expired_before_delivery_is_suppressed(runtime):
    """积压超过有效期的表单不再首发，也不产生执行或新的发送键。"""
    assert await channel(runtime).process(message("/skills"), Replies()) == "skill_form"
    with runtime.turns.sessions.begin() as session:
        view = session.scalar(select(Event).where(Event.kind == "skill_form"))
        view.payload = {**view.payload, "expires_at": (now() - timedelta(seconds=1)).isoformat()}
    gateway = Gateway()
    await drain(runtime, gateway)
    assert not gateway.calls
    with runtime.turns.sessions() as session:
        assert session.scalar(select(Delivery)).status == "suppressed"
    assert_no_task(runtime)
