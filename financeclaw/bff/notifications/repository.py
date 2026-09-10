"""通知订阅消费与发送租约；网络调用始终在事务外，回执由 epoch 保护。"""

import json
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.orm import aliased

from financeclaw.bff.application.feishu_cards import render_card
from financeclaw.bff.notifications.rendering import chunks
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.execution_ledger.repository import digest
from financeclaw.shared.execution_ledger.run_tables import RootRunRow
from financeclaw.shared.notifications.facts import require_schema, target_valid
from financeclaw.shared.notifications.tables import (
    NotificationDeliveryRow as Delivery,
)
from financeclaw.shared.notifications.tables import (
    NotificationEventRow as Event,
)
from financeclaw.shared.notifications.tables import (
    NotificationSenderRow as Sender,
)
from financeclaw.shared.notifications.tables import (
    NotificationTargetRow as Target,
)
from financeclaw.shared.notifications.tables import (
    utcnow,
)


def aware(value):
    """SQLite 时间与 PostgreSQL 使用相同 UTC 语义。"""
    from datetime import UTC

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class StaleSender(RuntimeError):
    """旧发送者只能丢弃回执，不能覆盖新租约或结论。"""


class NotificationRepository:
    """一次领取一个分片，每个事件独立消费，避免审计成功标记吞掉通知。"""

    def __init__(self, sessions, *, app_id, allowed_open_ids):
        """发送角色只处理自己应用及当前白名单内的已验证目标。"""
        self.sessions, self.app_id, self.allowed_open_ids = sessions, app_id, allowed_open_ids

    def materialize(self) -> bool:
        """事件消费标记与全部固定分片同事务，宕机重启不换内容和发送键。"""
        with self.sessions.begin() as session:
            event = session.scalar(
                select(Event)
                .join(Target)
                .where(Target.app_id == self.app_id, Event.materialized_at.is_(None))
                .order_by(Event.created_at, Event.event_id)
                .limit(1)
                .with_for_update(skip_locked=True, of=Event)
            )
            if event is None:
                return False
            if event.kind == "card" and session.scalar(
                select(Event.event_id)
                .where(
                    Event.target_id == event.target_id,
                    Event.kind == "card",
                    Event.revision > event.revision,
                )
                .limit(1)
            ):
                event.materialized_at = utcnow()
                return True
            if event.kind == "card":
                parts = [json.dumps(render_card(event.event_id, event.payload), ensure_ascii=False)]
            else:
                parts = chunks(event.payload["content"] or "处理已完成。")
            for index, content in enumerate(parts):
                identity = digest([event.target_id, event.event_id, 1, index])
                session.add(
                    Delivery(
                        delivery_id=identity,
                        event_id=event.event_id,
                        part=index,
                        parts=len(parts),
                        content=content,
                        content_hash=digest(content),
                        message_type="card" if event.kind == "card" else "text",
                        send_key=str(uuid5(NAMESPACE_URL, "financeclaw:notification:" + identity)),
                    )
                )
            event.materialized_at = utcnow()
            return True

    def claim(self, owner, *, lease_seconds):
        """SKIP LOCKED 只领当前可发送分片；丢失 sending 进程一律进入 uncertain。"""
        earlier = aliased(Delivery)
        prior_event = aliased(Event)
        prior_delivery = aliased(Delivery)
        with self.sessions.begin() as session:
            row = session.scalar(
                select(Delivery)
                .join(Event)
                .join(Target)
                .where(
                    Target.app_id == self.app_id,
                    Delivery.status.in_(["pending", "retry", "sending", "uncertain"]),
                    Delivery.due_at <= utcnow(),
                    or_(Delivery.lease_until.is_(None), Delivery.lease_until <= utcnow()),
                    ~exists(
                        select(earlier.delivery_id).where(
                            earlier.event_id == Delivery.event_id,
                            earlier.part < Delivery.part,
                            earlier.status.not_in(["sent", "suppressed", "dead_letter"]),
                        )
                    ),
                    # 同根任务的卡片更新串行；未取得明确回执前不会越过首发。
                    ~exists(
                        select(prior_event.event_id)
                        .outerjoin(prior_delivery, prior_delivery.event_id == prior_event.event_id)
                        .where(
                            Event.kind == "card",
                            prior_event.kind == "card",
                            prior_event.target_id == Event.target_id,
                            prior_event.revision < Event.revision,
                            or_(
                                prior_event.materialized_at.is_(None),
                                prior_delivery.status.not_in(["sent", "suppressed", "dead_letter"]),
                            ),
                        )
                    ),
                )
                .order_by(Event.created_at, Event.event_id, Delivery.part)
                .limit(1)
                .with_for_update(skip_locked=True, of=Delivery)
            )
            if row is None:
                return None
            if row.status == "sending":
                row.status, row.uncertain, row.error_class = "uncertain", True, "sender_lost"
            row.owner, row.epoch = owner, row.epoch + 1
            row.lease_until = utcnow() + timedelta(seconds=lease_seconds)
            target = session.get(Target, session.get(Event, row.event_id).target_id)
            if row.message_type == "card":
                if row.card_id is None and target.card_message_id:
                    row.card_id = target.card_id
                if row.first_attempt_at is None:
                    row.target_message_id = target.card_message_id
            return {
                "delivery_id": row.delivery_id,
                "owner": owner,
                "epoch": row.epoch,
                "address": dict(target.address),
                "content": row.content,
                "send_key": row.send_key,
                "message_type": row.message_type,
                "card_id": row.card_id,
                "target_message_id": row.target_message_id,
                "sequence": session.get(Event, row.event_id).revision,
            }

    @staticmethod
    def _locked(session, claim):
        """写回前校验仍有效的原始租约；过期不能自行续回。"""
        row = session.scalar(
            select(Delivery)
            .where(Delivery.delivery_id == claim["delivery_id"])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            row is None
            or row.owner != claim["owner"]
            or row.epoch != claim["epoch"]
            or row.lease_until is None
            or aware(row.lease_until) <= utcnow()
        ):
            raise StaleSender("notification lease expired")
        return row

    def _valid(self, session, row):
        """发送前复核当前绑定、取消和交互生命周期；不在查询中改变业务状态。"""
        event = session.get(Event, row.event_id)
        target = session.get(Target, event.target_id)
        if (
            target.app_id != self.app_id
            or target.address["open_id"] not in self.allowed_open_ids
            or not target_valid(session, target, require_active=event.kind != "card")
        ):
            return False
        if event.kind == "card":
            if not target.active and not target.card_message_id:
                return False
            if row.first_attempt_at is None:
                root = session.get(RootRunRow, target.run_id)
                if event.revision < root.revision:
                    return False
                item = event.payload.get("interaction")
                if item:
                    interaction = session.get(PendingInteractionRow, item["interaction_id"])
                    if (
                        interaction is None
                        or interaction.status != "pending"
                        or aware(interaction.expires_at) <= utcnow()
                    ):
                        return False
        return not session.scalar(
            select(Delivery.delivery_id)
            .where(
                Delivery.event_id == row.event_id,
                Delivery.part < row.part,
                Delivery.status != "sent",
            )
            .limit(1)
        )

    def validate(self, claim):
        """创建卡片等任何外部调用前，先拒绝损坏内容与已过时视图。"""
        with self.sessions.begin() as session:
            row = self._locked(session, claim)
            if row.content != claim["content"] or row.content_hash != digest(row.content):
                self._finish(
                    row, "uncertain" if row.uncertain else "dead_letter", "fixed_delivery_conflict"
                )
                return False
            if not self._valid(session, row):
                self._finish(row, "suppressed", "target_or_event_obsolete")
                return False
            return True

    def prepare(self, claim, *, recovery_seconds, timeout_seconds, recovery_evidence=None):
        """先持久化 sending，再做唯一网络调用；没有验证过的窗口时不重发未知结果。"""
        with self.sessions.begin() as session:
            row = self._locked(session, claim)
            target = session.get(Target, session.get(Event, row.event_id).target_id)
            if (
                row.content != claim["content"]
                or row.content_hash != digest(row.content)
                or row.send_key != claim["send_key"]
                or target.address != claim["address"]
                or row.message_type != claim["message_type"]
                or row.card_id != claim["card_id"]
                or row.target_message_id != claim["target_message_id"]
            ):
                self._finish(
                    row, "uncertain" if row.uncertain else "dead_letter", "fixed_delivery_conflict"
                )
                return False
            if not self._valid(session, row):
                self._finish(row, "suppressed", "target_or_event_obsolete")
                return False
            if row.uncertain and (
                row.recover_until is None
                or aware(row.recover_until) <= utcnow() + timedelta(seconds=timeout_seconds + 5)
            ):
                self._finish(row, "uncertain", "receipt_reconciliation_required")
                return False
            if row.first_attempt_at is None:
                if recovery_seconds and not recovery_evidence:
                    raise ValueError("dedup recovery requires verified evidence")
                row.first_attempt_at = utcnow()
                row.recovery_evidence_hash = digest(recovery_evidence) if recovery_seconds else None
                row.recover_until = (
                    utcnow() + timedelta(seconds=recovery_seconds) if recovery_seconds else None
                )
            row.attempts += 1
            row.status, row.updated_at = "sending", utcnow()
            return True

    def attach_card(self, claim, card_id):
        """在回复消息前持久绑定实例，孤立创建不能成为另一个发布目标。"""
        if not isinstance(card_id, str) or not 1 <= len(card_id) <= 128:
            raise ValueError("invalid card receipt")
        with self.sessions.begin() as session:
            row = self._locked(session, claim)
            target = session.get(Target, session.get(Event, row.event_id).target_id)
            if target.card_message_id and target.card_id != card_id:
                raise StaleSender("card instance was already bound")
            target.card_id = row.card_id = card_id
            claim["card_id"] = card_id

    @staticmethod
    def _finish(row, status, error=None, *, delay=None):
        """保留证据并释放租约；due_at=NULL 代表需人工核对或已终结。"""
        row.status, row.error_class = status, error
        row.owner, row.lease_until = None, None
        row.due_at = utcnow() + timedelta(seconds=delay) if delay is not None else None
        row.updated_at = utcnow()

    def settle(self, claim, receipt, *, max_failures):
        """明确拒绝可重试；先前未知投递不能被后来的明确拒绝抹除。"""
        with self.sessions.begin() as session:
            row = self._locked(session, claim)
            if receipt.status == "sent" and receipt.message_id:
                row.message_id, row.uncertain = receipt.message_id, False
                if row.message_type == "card":
                    event = session.get(Event, row.event_id)
                    target = session.get(Target, event.target_id)
                    if target.card_message_id and target.card_message_id != receipt.message_id:
                        raise StaleSender("card message receipt changed")
                    target.card_message_id = receipt.message_id
                    target.card_sequence = max(target.card_sequence, event.revision)
                self._finish(row, "sent")
            elif receipt.status == "uncertain" or row.uncertain:
                row.uncertain = True
                delay = 5 if row.recover_until and aware(row.recover_until) > utcnow() else None
                self._finish(row, "uncertain", receipt.error_class, delay=delay)
            elif receipt.status == "suppressed":
                self._finish(row, "suppressed", receipt.error_class)
            else:
                row.failures += 1
                retry = receipt.status == "retry" and row.failures < max_failures
                self._finish(
                    row,
                    "retry" if retry else "dead_letter",
                    receipt.error_class,
                    delay=min(60, 2**row.failures) if retry else None,
                )

    def renew(self, claim, *, lease_seconds):
        """慢读取和发送期间续租，过期写入仍被 fencing 拒绝。"""
        with self.sessions.begin() as session:
            result = session.execute(
                update(Delivery)
                .where(
                    Delivery.delivery_id == claim["delivery_id"],
                    Delivery.owner == claim["owner"],
                    Delivery.epoch == claim["epoch"],
                    Delivery.lease_until > utcnow(),
                )
                .values(lease_until=utcnow() + timedelta(seconds=lease_seconds))
            )
            return result.rowcount == 1

    def heartbeat(self, worker_id):
        """独立发送角色就绪事实不依赖 BFF lifespan。"""
        with self.sessions.begin() as session:
            session.merge(
                Sender(worker_id=worker_id, app_id=self.app_id, version=1, heartbeat_at=utcnow())
            )

    def healthy(self, *, maximum_age):
        """只读检查 schema 与当前应用的兼容发送者心跳。"""
        require_schema(self.sessions)
        with self.sessions() as session:
            return (
                session.scalar(
                    select(Sender.worker_id)
                    .where(
                        Sender.app_id == self.app_id,
                        Sender.version == 1,
                        Sender.heartbeat_at > utcnow() - timedelta(seconds=maximum_age),
                    )
                    .limit(1)
                )
                is not None
            )

    def metrics(self):
        """有界分类计数和最早积压时间，不输出目标、正文或原始 SDK 错误。"""
        with self.sessions() as session:
            rows = session.execute(
                select(Delivery.status, func.count(), func.min(Delivery.updated_at))
                .join(Event)
                .join(Target)
                .where(Target.app_id == self.app_id)
                .group_by(Delivery.status)
            )
            return {
                status: {"count": count, "oldest": oldest.isoformat()}
                for status, count, oldest in rows
            }
