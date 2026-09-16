"""飞书技能表单的持久展示与一次性任务受理，沿用通知和 Turn 事务。"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update

from financeclaw.kernel.authorization import AuthorizationEvidence
from financeclaw.kernel.responses import ConversationTurnRequest
from financeclaw.kernel.skills import SkillError
from financeclaw.shared.channels.feishu.cards import toast
from financeclaw.shared.channels.feishu.skill_cards import (
    SkillFormError,
    render_skill_form,
    skill_form_selection,
)
from financeclaw.shared.conversation.repository import ConversationConflict
from financeclaw.shared.conversation.tables import ChannelConversationBindingRow, ConversationRow
from financeclaw.shared.notifications.facts import record_progress, target_valid
from financeclaw.shared.notifications.tables import NotificationEventRow, NotificationTargetRow
from financeclaw.shared.turns.receipts import read_receipt, save_receipt
from financeclaw.shared.turns.tables import ConversationTurnRow
from financeclaw.shared.turns.types import TERMINAL_STATUSES, ExecutionConflict, aware, digest, now

FORM_LIFETIME = timedelta(minutes=30)


@dataclass(frozen=True)
class SkillFormActor:
    """受信任渠道身份的可见性输入，展示阶段尚无执行上下文或 Turn。"""

    tenant_id: str
    subject_id: str
    scopes: frozenset[str]


def create_skill_form(service, message, conversation_id, *, app_id, scopes):
    """只提交无 Turn 的通知目标和冻结表单；重复消息复用原表单与发送键。"""
    tenant_id, subject_id = f"feishu:{message.tenant_key}", f"feishu:{message.sender_open_id}"
    address = {
        "channel": "feishu",
        "app_id": app_id,
        "tenant_key": message.tenant_key,
        "open_id": message.sender_open_id,
        "chat_id": message.chat_id,
        "message_id": message.message_id,
    }
    target_id = digest(["feishu:skill-form", app_id, message.message_id])
    with service.sessions.begin() as session:
        session.execute(
            update(ConversationRow)
            .where(ConversationRow.conversation_id == conversation_id)
            .values(updated_at=ConversationRow.updated_at)
        )
        conversation = session.get(ConversationRow, conversation_id)
        if (
            conversation is None
            or (conversation.tenant_id, conversation.subject_id) != (tenant_id, subject_id)
            or conversation.status != "active"
        ):
            raise ConversationConflict("skill form requires an owned active conversation")
        existing = session.get(NotificationTargetRow, target_id)
        if existing is not None:
            if existing.address != address or not target_valid(
                session, existing, require_active=False
            ):
                raise ExecutionConflict("skill form message identity changed")
            return True
        try:
            profile = service.releases.agents.resolve(
                conversation.agent_id, conversation.agent_profile_version
            )
        except LookupError as exc:
            active = session.scalar(
                select(ConversationTurnRow.turn_id)
                .where(
                    ConversationTurnRow.conversation_id == conversation_id,
                    ConversationTurnRow.status.not_in(TERMINAL_STATUSES),
                )
                .limit(1)
            )
            hint = (
                "当前会话仍有未完成的旧版本任务，请先停止该任务，再重新发送 /skills。"
                if active
                else "当前会话与服务版本不一致，请稍后重试 /skills。"
            )
            raise SkillFormError(hint) from exc
        service.releases.require_root(profile)
        if "*" not in scopes and not profile.required_scopes.issubset(scopes):
            return False
        context = SkillFormActor(tenant_id=tenant_id, subject_id=subject_id, scopes=scopes)
        options = []
        for ref in profile.allowed_skills:
            try:
                release, _ = service.releases.skills.authorize(
                    profile, context, ref.skill_id, explicit=True, invoke=True
                )
            except SkillError:
                continue
            options.append(
                {
                    "ref": ref.model_dump(mode="json"),
                    "policy_hash": release.policy_hash,
                    "label": release.display_name or ref.skill_id,
                }
            )
        if not options:
            return False
        binding = session.scalar(
            select(ChannelConversationBindingRow).where(
                ChannelConversationBindingRow.conversation_id == conversation_id,
                ChannelConversationBindingRow.channel == "feishu",
                ChannelConversationBindingRow.app_id == app_id,
                ChannelConversationBindingRow.tenant_key == message.tenant_key,
                ChannelConversationBindingRow.external_chat_id == message.chat_id,
                ChannelConversationBindingRow.external_user_id == message.sender_open_id,
            )
        )
        if binding is None:
            raise ExecutionConflict("skill form requires a verified channel binding")
        payload = {
            "kind": "skill_form",
            "conversation_id": conversation_id,
            "profile_hash": digest(profile.model_dump(mode="json")),
            "skills": options,
            "expires_at": (now() + FORM_LIFETIME).isoformat(),
            "card_revision": 1,
        }
        session.add(
            NotificationTargetRow(
                target_id=target_id,
                binding_id=binding.binding_id,
                tenant_id=tenant_id,
                subject_id=subject_id,
                app_id=app_id,
                address=address,
                card_payload=payload,
            )
        )
        session.flush()
        session.add(
            NotificationEventRow(
                event_id=digest([target_id, "skill_form"]),
                target_id=target_id,
                revision=1,
                kind="skill_form",
                payload=payload,
            )
        )
        return True


def accept_skill_form(actions, session, view, *, tenant, operator, context, value, form, event_key):
    """同一事务固定输入、创建 Turn、绑定原卡并保存回执；失败仍保持未提交表单。"""
    from financeclaw.api.application.feishu_card_actions import button_values

    service = actions.runs
    conversation_id = view.payload["conversation_id"]
    # 与普通 Turn 受理统一先锁 Conversation，避免表单与任务并发出现相反锁序。
    session.execute(
        update(ConversationRow)
        .where(ConversationRow.conversation_id == conversation_id)
        .values(updated_at=ConversationRow.updated_at)
    )
    target = session.scalar(
        select(NotificationTargetRow)
        .where(NotificationTargetRow.target_id == view.target_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        target is None
        or target.app_id != actions.app_id
        or target.tenant_id != f"feishu:{tenant}"
        or target.subject_id != f"feishu:{operator['open_id']}"
        or target.address["chat_id"] != context["open_chat_id"]
        or not target_valid(session, target, require_active=False)
        or value not in list(button_values(render_skill_form(view.event_id, view.payload)))
    ):
        raise ValueError("skill form does not belong to this channel callback")
    if not target.card_message_id:
        return toast("表单正在完成投递确认，请稍后再点一次。", "warning")
    if target.card_message_id != context["open_message_id"]:
        raise ValueError("skill form message mismatch")
    selected, task = skill_form_selection(view.payload, form)
    wire_hash = digest([value, form, context, operator["open_id"]])
    receipt_key = f"feishu:event:{actions.app_id}:{event_key}"
    intent_key = "feishu:skill-task:" + target.target_id
    fingerprint = digest([selected, task])
    if target.turn_id:
        root = service.store.lock(session, target.turn_id)
        previous = read_receipt(session, root, receipt_key, wire_hash)
        if previous is not None:
            return previous
        previous = read_receipt(session, root, intent_key, fingerprint)
        if previous is None:
            raise ExecutionConflict("submitted skill form has no committed receipt")
        save_receipt(session, root, receipt_key, wire_hash, previous)
        return previous
    if not target.active or aware(datetime.fromisoformat(view.payload["expires_at"])) <= now():
        raise SkillFormError("表单已过期，请重新发送 /skills。")
    conversation = session.get(ConversationRow, conversation_id)
    try:
        profile = service.releases.agents.resolve(
            conversation.agent_id, conversation.agent_profile_version
        )
    except LookupError as exc:
        raise SkillFormError("可用技能已更新，请重新发送 /skills。") from exc
    if digest(profile.model_dump(mode="json")) != view.payload["profile_hash"]:
        raise SkillFormError("可用技能已更新，请重新发送 /skills。")
    ctx = SkillFormActor(
        tenant_id=target.tenant_id,
        subject_id=target.subject_id,
        scopes=actions.scopes,
    )
    try:
        release, _ = service.releases.skills.authorize(
            profile, ctx, selected["ref"]["skill_id"], explicit=True, invoke=True
        )
        if (
            release.ref.model_dump(mode="json") != selected["ref"]
            or release.policy_hash != selected["policy_hash"]
        ):
            raise SkillError()
    except SkillError as exc:
        raise SkillFormError("该技能当前不可用，请重新发送 /skills 选择。") from exc
    current = now()
    evidence = AuthorizationEvidence(
        source="feishu",
        source_hash=digest(
            [actions.app_id, tenant, operator["open_id"], context["open_chat_id"], event_key]
        ),
        issued_at=current,
        expires_at=current + timedelta(seconds=service.settings.turn_grant_seconds),
    )
    try:
        accepted = service.admission.accept(
            conversation_id,
            ConversationTurnRequest(message=f"/skill {release.ref.skill_id} {task}"),
            tenant_id=target.tenant_id,
            subject_id=target.subject_id,
            scopes=actions.scopes,
            idempotency_key=intent_key,
            authorization=evidence,
            transaction=session,
        )
    except ConversationConflict as exc:
        raise SkillFormError("上一条任务仍在处理中，请完成或停止后再提交。") from exc
    target.turn_id = accepted.turn_id
    target.card_payload = {
        "card_revision": view.revision,
        "task": task,
        "skill_name": selected["label"],
    }
    session.flush()
    root = service.store.lock(session, accepted.turn_id)
    record_progress(session, root)
    result = toast(f"已受理 · {selected['label']}\n任务编号：{accepted.turn_id}")
    save_receipt(session, root, intent_key, fingerprint, result)
    save_receipt(session, root, receipt_key, wire_hash, result)
    return result
