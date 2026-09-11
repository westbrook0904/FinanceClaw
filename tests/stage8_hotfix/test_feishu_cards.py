"""任务卡经真实 BFF 事务和通知账本闭环；仅替换外部飞书传输。"""

import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from financeclaw.api.application.feishu_card_actions import button_values
from financeclaw.integrations.notifications.repository import NotificationRepository
from financeclaw.integrations.notifications.worker import deliver
from financeclaw.kernel.interactions import InteractionPoint
from financeclaw.shared.channels.feishu.cards import parse_fields, render_card
from financeclaw.shared.notifications.tables import NotificationEventRow as Event
from financeclaw.shared.notifications.tables import NotificationTargetRow as Target
from financeclaw.shared.turns.tables import ConversationTurnRow, InteractionRow, TurnCommandRow
from financeclaw.shared.turns.types import now
from tests.stage8.test_notifications import Gateway
from tests.stage8_hotfix.test_feishu_clarification import (
    OWNER,
    Replies,
    ask,
    channel,
    message,
    waiting,
)
from tests.stage10.runtime import runtime as runtime
from tests.stage10.runtime import tick


async def publish(runtime, gateway=None):
    """消费所有已有快照，返回最后一份已确认送达的任务卡。"""
    repository = NotificationRepository(
        runtime.turns.store.sessions, app_id="app", allowed_open_ids=frozenset({"user"})
    )
    while repository.materialize():
        pass
    gateway = gateway or Gateway()
    for _ in range(30):
        claim = repository.claim("synthetic-sender", lease_seconds=60)
        if claim is None:
            break
        await deliver(repository, gateway, claim, runtime.turns.settings)
    else:
        raise AssertionError("notification loop did not drain")
    with repository.sessions() as session:
        target = session.scalar(select(Target))
        assert target.card_message_id
        event = session.scalar(
            select(Event).where(
                Event.target_id == target.target_id,
                Event.kind == "card",
                Event.revision == target.card_sequence,
            )
        )
        return event, gateway


def callback(event, op, *, event_id="event-1", form=None, option=None):
    """由实际可见按钮生成飞书 SDK 的原始 callback 信封。"""
    value = next(
        v
        for v in button_values(render_card(event.event_id, event.payload))
        if v["op"] == op and (option is None or v.get("option") == option)
    )
    return {
        "schema": "2.0",
        "header": {
            "app_id": "app",
            "tenant_key": "tenant",
            "event_type": "card.action.trigger",
            "event_id": event_id,
        },
        "event": {
            "operator": {"open_id": "user", "tenant_key": "tenant"},
            "context": {"open_chat_id": "chat", "open_message_id": "message-1"},
            "action": {"value": value, "form_value": form or {}},
        },
    }


async def fresh(runtime):
    """经过飞书入口受理，尚未启动后端就可看到停止按钮。"""
    service = channel(runtime)
    assert await service.process(message(), Replies()) == "accepted"
    with runtime.turns.store.sessions() as session:
        root = session.scalar(select(ConversationTurnRow))
        accepted = SimpleNamespace(turn_id=root.turn_id, thread_id=root.thread_id)
    event, gateway = await publish(runtime)
    return service, accepted, event, gateway


@pytest.mark.asyncio
async def test_stop_before_dispatch_is_durable_and_allows_next_turn(runtime):
    """停止先落库，排空后可在同一单聊开启下一轮。"""
    service, accepted, event, gateway = await fresh(runtime)
    raw = callback(event, "cancel")
    first = await service.card_actions.handle(raw)
    assert first["toast"]["content"] == "停止请求已受理"
    assert await channel(runtime).card_actions.handle(raw) == first
    assert (await runtime.turns.status(accepted.turn_id, **OWNER)).status == "cancelling"
    await tick(runtime)
    assert not runtime.client.runs.calls
    assert (await runtime.turns.status(accepted.turn_id, **OWNER)).status == "cancelled"
    final, _ = await publish(runtime, gateway)
    assert not list(button_values(render_card(final.event_id, final.payload)))
    assert len({c["target_message_id"] for c in gateway.calls[1:]}) == 1
    assert await service.process(message("下一轮", identifier="next"), Replies()) == "accepted"


@pytest.mark.asyncio
async def test_stop_running_turn_cancels_exact_remote_attempt(runtime):
    """停止只指向已绑定的远端运行，不创建替代运行。"""
    service, accepted, event, gateway = await fresh(runtime)
    await tick(runtime)
    native_id = next(iter(runtime.client.runs.values))
    assert (await service.card_actions.handle(callback(event, "cancel")))["toast"]["type"] == "info"
    await tick(runtime)
    assert runtime.client.cancelled == [native_id]
    assert (await runtime.turns.status(accepted.turn_id, **OWNER)).status == "cancelled"


@pytest.mark.asyncio
async def test_answer_commits_before_ack_and_replays_cannot_answer_next_question(runtime):
    """重复事件和再次点击复用回执，后续问题不会被旧输入回答。"""
    service, _, accepted, item = await waiting(runtime)
    event, gateway = await publish(runtime)
    raw = callback(event, "answer", form={"f0": "当地钟表时间"})
    result = await service.card_actions.handle(raw)
    assert result["toast"]["type"] == "info"
    with runtime.turns.store.sessions() as session:
        row = session.get(InteractionRow, item["interaction_id"])
        assert row.response["answer"] == {"text": "当地钟表时间"}
        operation_id = row.resume_command_id
    raw["header"]["event_id"] = "duplicate-physical-click"
    assert await channel(runtime).card_actions.handle(raw) == result
    await tick(runtime)
    second = await ask(runtime, accepted, native_id="new-question")
    assert await service.card_actions.handle(raw) == result
    changed = copy.deepcopy(raw)
    changed["header"]["event_id"] = "changed-answer"
    changed["event"]["action"]["form_value"] = {"f0": "真太阳时"}
    assert (await service.card_actions.handle(changed))["toast"]["type"] == "error"
    with runtime.turns.store.sessions() as session:
        assert session.get(InteractionRow, second["interaction_id"]).response is None
        assert session.get(InteractionRow, item["interaction_id"]).resume_command_id == operation_id
    current, _ = await publish(runtime, gateway)
    assert current.payload["interaction"]["interaction_id"] == second["interaction_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_approval_binds_visible_action_and_creates_one_resume(runtime, decision):
    """批准和拒绝互斥，决定绑定卡片展示的完整动作。"""
    service, _, _, item = await waiting(runtime, approval=True)
    event, _ = await publish(runtime)
    raw = callback(event, decision, form={"reason": "已核对"})
    assert (await service.card_actions.handle(raw))["toast"]["type"] == "info"
    raw["header"]["event_id"] = "another-click"
    assert (await service.card_actions.handle(raw))["toast"]["type"] == "info"
    opposite = callback(
        event, "reject" if decision == "approve" else "approve", event_id="opposite"
    )
    assert (await service.card_actions.handle(opposite))["toast"]["type"] == "error"
    with runtime.turns.store.sessions() as session:
        response = session.get(InteractionRow, item["interaction_id"]).response
        assert response["decision"] == decision and response["action_hash"] == item["action_hash"]
        assert session.scalar(select(func.count()).select_from(TurnCommandRow)) == 2


@pytest.mark.asyncio
async def test_revoke_and_reauthorize_replay_never_extends_grant(runtime):
    """撤权与授权按钮各产生一次有限授权变更。"""
    service, accepted, event, gateway = await fresh(runtime)
    revoke = callback(event, "revoke")
    assert (await service.card_actions.handle(revoke))["toast"]["type"] == "info"
    with runtime.turns.store.sessions() as session:
        assert session.get(ConversationTurnRow, accepted.turn_id).grant_revoked
    revoked, _ = await publish(runtime, gateway)
    authorize = callback(revoked, "authorize", event_id="authorize")
    assert (await service.card_actions.handle(authorize))["toast"]["type"] == "info"
    with runtime.turns.store.sessions() as session:
        grant = session.get(ConversationTurnRow, accepted.turn_id)
        revision, expires = grant.grant_revision, grant.grant_expires_at
        assert not grant.grant_revoked
    authorize["header"]["event_id"] = "authorize-repeated"
    assert (await service.card_actions.handle(authorize))["toast"]["type"] == "info"
    assert (await service.card_actions.handle(revoke))["toast"]["type"] == "info"
    with runtime.turns.store.sessions() as session:
        grant = session.get(ConversationTurnRow, accepted.turn_id)
        assert (grant.grant_revision, grant.grant_expires_at, grant.grant_revoked) == (
            revision,
            expires,
            False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["app", "tenant", "user", "chat", "message", "value", "form"])
async def test_callback_rejects_forged_or_forwarded_context(runtime, field):
    """信任链任意一处变化均不能取消原任务。"""
    service, accepted, event, _ = await fresh(runtime)
    raw = callback(event, "cancel")
    if field == "app":
        raw["header"]["app_id"] = "other"
    elif field == "tenant":
        raw["event"]["operator"]["tenant_key"] = "other"
    elif field == "user":
        raw["event"]["operator"]["open_id"] = "other"
    elif field in {"chat", "message"}:
        raw["event"]["context"]["open_" + field + "_id"] = "other"
    elif field == "value":
        raw["event"]["action"]["value"]["scopes"] = ["admin"]
    else:
        raw["event"]["action"]["form_value"] = {"injected": "value"}
    assert (await service.card_actions.handle(raw))["toast"]["type"] == "error"
    assert (await runtime.turns.status(accepted.turn_id, **OWNER)).status == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", ["expired", "cancelled"])
async def test_old_answer_cannot_resume_expired_or_stopped_turn(runtime, closed):
    """等待点过期或被停止后不接受迟到回答。"""
    service, _, accepted, item = await waiting(runtime)
    event, _ = await publish(runtime)
    if closed == "expired":
        with runtime.turns.store.sessions.begin() as session:
            session.get(InteractionRow, item["interaction_id"]).expires_at = now() - timedelta(
                seconds=1
            )
    else:
        await service.card_actions.handle(callback(event, "cancel", event_id="stop"))
    assert (await service.card_actions.handle(callback(event, "answer", form={"f0": "迟到"})))[
        "toast"
    ]["type"] == "error"
    assert len(runtime.client.runs.calls) == 1


@pytest.mark.asyncio
async def test_callback_receipt_failure_rolls_back_decision_and_can_retry(runtime, monkeypatch):
    """回执与决定原子提交，提交前崩溃不会留下半个决定。"""
    import financeclaw.api.application.feishu_card_actions as actions

    service, _, _, item = await waiting(runtime)
    event, _ = await publish(runtime)
    raw = callback(event, "answer", form={"f0": "当地钟表时间"})
    original = actions.save_receipt

    def crash(*args):
        """在决定保存后、整个事务提交前注入失败。"""
        raise RuntimeError("synthetic crash before commit")

    monkeypatch.setattr(actions, "save_receipt", crash)
    assert (await service.card_actions.handle(raw))["toast"]["type"] == "warning"
    with runtime.turns.store.sessions() as session:
        assert session.get(InteractionRow, item["interaction_id"]).response is None
        assert session.scalar(select(func.count()).select_from(TurnCommandRow)) == 1
    monkeypatch.setattr(actions, "save_receipt", original)
    assert (await service.card_actions.handle(raw))["toast"]["type"] == "info"


@pytest.mark.asyncio
@pytest.mark.parametrize("multiple", [False, True])
async def test_choice_card_uses_frozen_options_and_business_validation(runtime, multiple):
    """卡片选择映射为发布选项，多选使用稳定顺序去重。"""
    service, _, accepted, item = await waiting(runtime)
    # 为这个已观察中断装入合成发布契约；恢复仍走生产 BFF 的同一受理事务。
    point = InteractionPoint(
        point_id="synthetic_choice",
        kind="choice",
        question="请选择",
        options=("甲", "乙", "丙", "丁"),
        selection_mode="multiple" if multiple else "single",
        min_selected=1,
        max_selected=2,
    )
    with runtime.turns.store.sessions.begin() as session:
        row = session.get(InteractionRow, item["interaction_id"])
        row.kind = "choice"
        row.request = {
            **row.request,
            "point": point.model_dump(mode="json"),
        }
        session.flush()
        root = runtime.turns.store.lock(session, accepted.turn_id)
        runtime.turns.store.transition(session, root, root.status, root.status_reason, changed=True)
    event, _ = await publish(runtime)
    bad = callback(
        event, "choose", form={"selection": ["o0", "o1", "o2"] if multiple else "unknown"}
    )
    assert (await service.card_actions.handle(bad))["toast"]["type"] == "error"
    raw = callback(
        event, "choose", event_id="valid", form={"selection": ["o2", "o0"] if multiple else "o1"}
    )
    assert (await service.card_actions.handle(raw))["toast"]["type"] == "info"
    if multiple:
        raw["header"]["event_id"] = "reordered"
        raw["event"]["action"]["form_value"]["selection"].reverse()
        assert (await service.card_actions.handle(raw))["toast"]["type"] == "info"
    with runtime.turns.store.sessions() as session:
        assert session.get(InteractionRow, item["interaction_id"]).response["answer"] == (
            ["甲", "丙"] if multiple else "乙"
        )


def test_typed_form_and_agent_resume_share_schema_validation(monkeypatch):
    """控件值显式还原类型，Agent 重入时再验证业务约束。"""
    from financeclaw.agent_server.tools import interaction

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "age", "enabled", "markets"],
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "age": {"type": "integer", "minimum": 1},
            "enabled": {"type": "boolean"},
            "markets": {
                "type": "array",
                "items": {"type": "string", "enum": ["A", "B"]},
                "minItems": 1,
            },
        },
    }
    fields = {"f0": "合成", "f1": "42", "f2": False, "f3": ["o1", "o0"]}
    answer = parse_fields(schema, fields)
    assert answer == {"name": "合成", "age": 42, "enabled": False, "markets": ["A", "B"]}
    point = InteractionPoint(point_id="form", kind="input", question="填写", response_schema=schema)
    monkeypatch.setattr(interaction, "interrupt", lambda _: {"kind": "input", "answer": answer})
    assert interaction.request_user_interaction(point)["answer"] == answer
    answer["age"] = 0
    from jsonschema import ValidationError

    with pytest.raises(ValidationError):
        interaction.request_user_interaction(point)
    with pytest.raises(ValueError):
        parse_fields(schema, {**fields, "f2": "false"})
    with pytest.raises(ValueError):
        parse_fields(schema, {**fields, "f1": "NaN"})
    with pytest.raises(ValueError):
        parse_fields(schema, {**fields, "unexpected": "x"})


@pytest.mark.parametrize("answer", [["甲", "甲"], ["未发布"], "甲", [], [True], ["甲", "乙", "丙"]])
def test_agent_rejects_invalid_multiple_choice(monkeypatch, answer):
    """Agent 不信任客户端已校验的声明，拒绝超界和重复选项。"""
    from financeclaw.agent_server.tools import interaction

    point = InteractionPoint(
        point_id="choice",
        kind="choice",
        question="选择",
        options=("甲", "乙", "丙"),
        selection_mode="multiple",
        max_selected=2,
    )
    monkeypatch.setattr(interaction, "interrupt", lambda _: {"kind": "choice", "answer": answer})
    with pytest.raises(ValueError):
        interaction.request_user_interaction(point)


def test_short_choice_buttons_have_unique_names_and_fixed_values():
    """少量短选项使用独立组件名称，回调仍绑定冻结选项编码。"""
    from financeclaw.shared.channels.feishu.cards import interaction_elements

    item = {
        "status": "pending",
        "kind": "choice",
        "selection_mode": "single",
        "question": "选择",
        "options": ["甲", "乙", "丙"],
    }
    controls = [node for node in interaction_elements("view", item) if node["tag"] == "button"]
    assert len({node["name"] for node in controls}) == 3
    assert [node["value"]["option"] for node in controls] == ["o0", "o1", "o2"]
