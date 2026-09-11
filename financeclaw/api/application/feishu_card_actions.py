"""可信卡片回调的短事务受理，不经过消息队列或模型解释。"""

import json
import logging
import time
from datetime import timedelta

from sqlalchemy import text

from financeclaw.api.application.turns.controls import apply_control
from financeclaw.kernel.authorization import AuthorizationEvidence
from financeclaw.shared.channels.feishu.cards import render_card, response_from_card, toast
from financeclaw.shared.infrastructure.asyncio import run_sync
from financeclaw.shared.notifications.facts import target_valid
from financeclaw.shared.notifications.tables import NotificationEventRow, NotificationTargetRow
from financeclaw.shared.turns.receipts import read_receipt, save_receipt
from financeclaw.shared.turns.types import ExecutionConflict, digest, now

LOGGER = logging.getLogger(__name__)


def button_values(node):
    """列出冻结视图里真实提供的按钮，拒绝客户端构造额外动作。"""
    if isinstance(node, dict):
        if node.get("tag") == "button" and "value" in node:
            yield node["value"]
        for child in node.values():
            yield from button_values(child)
    elif isinstance(node, list):
        for child in node:
            yield from button_values(child)


class FeishuCardActions:
    """复用通知视图、根锁和 Inbox 回执；成功回包之前业务已持久化。"""

    def __init__(self, runs, *, app_id, allowed_open_ids, scopes):
        """回调权限由配置提供，不能由卡片 value 授予。"""
        self.runs, self.app_id = runs, app_id
        self.allowed_open_ids, self.scopes = allowed_open_ids, scopes

    async def handle(self, raw):
        """在工作线程完成有界数据库操作，保持 API 事件循环可用。"""
        try:
            result = await run_sync(self.accept, raw)
            if self.runs.lifecycle:
                self.runs.lifecycle.wake()
            return result
        except (ValueError, KeyError, TypeError, ExecutionConflict) as exc:
            LOGGER.info("Feishu card action rejected", extra={"error_type": type(exc).__name__})
            return toast("未受理：请检查输入，并使用原单聊中的最新卡片。", "error")
        except Exception as exc:
            LOGGER.warning(
                "Feishu card acceptance unconfirmed", extra={"error_type": type(exc).__name__}
            )
            return toast("暂时无法确认受理结果，请稍后重试同一操作。", "warning")

    def accept(self, raw):
        """仅接收 SDK 已验证的事件，决定、回执和展示更新共用根锁事务。"""
        started = time.monotonic()
        if not isinstance(raw, dict) or len(json.dumps(raw, ensure_ascii=False).encode()) > 65536:
            raise ValueError("callback too large")
        header, event = raw["header"], raw["event"]
        operator, context, action = event["operator"], event["context"], event["action"]
        tenant = operator.get("tenant_key") or header.get("tenant_key")
        if header.get("tenant_key") and header["tenant_key"] != tenant:
            raise ValueError("tenant mismatch")
        if header.get("app_id") != self.app_id or header.get("event_type") != "card.action.trigger":
            raise ValueError("application or event mismatch")
        if not tenant or operator["open_id"] not in self.allowed_open_ids:
            raise ValueError("operator not allowed")
        event_key = header["event_id"]
        if not isinstance(event_key, str) or not 1 <= len(event_key) <= 128:
            raise ValueError("missing event identity")
        value, form = action["value"], action.get("form_value") or {}
        if not isinstance(value, dict) or not isinstance(form, dict):
            raise ValueError("invalid callback fields")
        if not isinstance(value.get("view"), str) or len(value["view"]) != 64:
            raise ValueError("invalid card view")
        with self.runs.store.sessions.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET LOCAL lock_timeout = '750ms'"))
                session.execute(text("SET LOCAL statement_timeout = '1200ms'"))
            view = session.get(NotificationEventRow, value["view"])
            if view is None or view.kind != "card":
                raise ValueError("unknown view")
            target = session.get(NotificationTargetRow, view.target_id)
            root = self.runs.store.lock(session, target.turn_id)
            # 根锁之后刷新发送回执与绑定，避免读取到撤权或消息发送前的旧状态。
            session.refresh(target)
            if (
                target.app_id != self.app_id
                or target.tenant_id != f"feishu:{tenant}"
                or target.subject_id != f"feishu:{operator['open_id']}"
                or target.address["chat_id"] != context["open_chat_id"]
                or not target_valid(session, target, require_active=False)
            ):
                raise ValueError("callback does not belong to original conversation")
            if not target.card_message_id:
                return toast("卡片正在完成投递确认，请稍后再点一次。", "warning")
            if context["open_message_id"] != target.card_message_id:
                raise ValueError("callback message mismatch")
            if value not in list(button_values(render_card(view.event_id, view.payload))):
                raise ValueError("button is not in the reviewed card")
            wire_hash = digest([value, form, context, operator["open_id"]])
            receipt_key = f"feishu:event:{self.app_id}:{event_key}"
            replay = read_receipt(session, root, receipt_key, wire_hash)
            if replay is not None:
                return replay
            op = value["op"]
            response = None
            if op in {"answer", "choose", "approve", "reject"}:
                response = response_from_card(view.payload, value, form)
                # 与业务受理使用相同的多选规范化。
                if response.kind == "choice" and isinstance(response.answer, list):
                    response = response.model_copy(
                        update={
                            "answer": [
                                o
                                for o in view.payload["interaction"]["options"]
                                if o in response.answer
                            ]
                        }
                    )
            elif form:
                raise ValueError("task control has no form")
            intent_key = "feishu:intent:" + digest([view.event_id, op, value.get("option")])
            fingerprint = digest(response.model_dump(mode="json") if response else value)
            replay = read_receipt(session, root, intent_key, fingerprint)
            if replay is not None:
                save_receipt(session, root, receipt_key, wire_hash, replay)
                return replay
            current = now()
            evidence = AuthorizationEvidence(
                source="feishu",
                source_hash=digest(
                    [self.app_id, tenant, operator["open_id"], context["open_chat_id"], event_key]
                ),
                issued_at=current,
                expires_at=current + timedelta(seconds=self.runs.settings.turn_grant_seconds),
            )
            if response:
                self.runs.interactions.accept_response(
                    view.payload["interaction"]["interaction_id"],
                    response,
                    tenant_id=target.tenant_id,
                    subject_id=target.subject_id,
                    scopes=self.scopes,
                    conversation_id=root.conversation_id,
                    idempotency_key=intent_key,
                    authorization=evidence,
                    session=session,
                )
            else:
                apply_control(
                    self.runs,
                    session,
                    root,
                    op,
                    scopes=self.scopes,
                    authorization=evidence,
                    revision=view.payload["grant"]["revision"],
                )
            result = toast(
                {
                    "cancel": "停止请求已受理",
                    "authorize": "授权已更新",
                    "revoke": "后台授权已撤销",
                    "reject": "拒绝已受理",
                }.get(op, "回答已受理")
            )
            save_receipt(session, root, intent_key, fingerprint, result)
            save_receipt(session, root, receipt_key, wire_hash, result)
            if time.monotonic() - started > 1.8:
                raise TimeoutError("callback transaction exceeded budget")
            return result
