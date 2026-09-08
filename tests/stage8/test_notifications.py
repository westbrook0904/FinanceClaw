"""8B 原子通知责任、独立发送恢复、固定目标和故障边界。"""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from financeclaw.bff.application.feishu_channel_service import (
    FeishuChannelService,
    FeishuInboundMessage,
)
from financeclaw.bff.notifications.feishu import Receipt
from financeclaw.bff.notifications.repository import NotificationRepository, StaleSender
from financeclaw.bff.notifications.worker import deliver
from financeclaw.coordination.repository import now
from financeclaw.shared.conversation.tables import (
    ChannelConversationBindingRow,
    ConversationMessageRow,
)
from financeclaw.shared.execution_ledger.coordination_tables import CoordinatedRunRow
from financeclaw.shared.execution_ledger.interaction_tables import PendingInteractionRow
from financeclaw.shared.notifications.tables import NotificationDeliveryRow as Delivery
from financeclaw.shared.notifications.tables import NotificationEventRow as Event
from financeclaw.shared.notifications.tables import NotificationTargetRow as Target
from tests.stage8.test_coordinator import tick


class NoDisplay:
    """协调通知开启后，BFF 不应再尝试任何展示或最终发送。"""

    async def stream_markdown(self, **kwargs):
        """拒绝创建第二份最终卡片。"""
        raise AssertionError("unexpected streaming display")

    async def send_text(self, **kwargs):
        """普通任务受理不在前台发送文本。"""
        raise AssertionError("unexpected foreground delivery")


class Gateway:
    """可控外部送达账本；首次送达后可丢响应，原键恢复不产生第二条消息。"""

    def __init__(self, *, lose=False, receipts=()):
        """脚本化失败与外部已发送消息分开存储。"""
        self.lose, self.receipts = lose, list(receipts)
        self.calls, self.messages = [], {}

    async def check_target(self, address):
        """当前测试目标仍有效；生产 SDK 另有协议测试。"""
        return None

    async def send(self, claim):
        """仿真远端固定键去重，调用账本包含每次真实尝试。"""
        self.calls.append(dict(claim))
        if self.receipts:
            return self.receipts.pop(0)
        self.messages.setdefault(claim["send_key"], "message-" + str(len(self.messages) + 1))
        if self.lose:
            self.lose = False
            raise TimeoutError("synthetic lost response")
        return Receipt("sent", message_id=self.messages[claim["send_key"]])


async def admitted(setup):
    """实际飞书应用受理即退出，后续通过独立 Coordinator 与 Sender 推进。"""
    setup.settings = setup.settings.model_copy(update={"feishu_notifications_enabled": True})
    setup.bff.runs.settings = setup.settings
    service = FeishuChannelService(
        setup.bff, app_id="app", allowed_open_ids=frozenset({"user"}), scopes=setup.scopes
    )
    message = FeishuInboundMessage(
        message_id="original",
        tenant_key="tenant",
        sender_open_id="user",
        chat_id="chat",
        chat_type="p2p",
        content_type="text",
        text="synthetic question",
        sender_type="user",
    )
    assert await service.process(message, NoDisplay()) == "accepted"
    with setup.store.sessions() as session:
        target = session.scalar(select(Target))
        run_id = target.run_id
    repository = NotificationRepository(
        setup.store.sessions, app_id="app", allowed_open_ids=frozenset({"user"})
    )
    return service, message, run_id, repository


async def completed(setup):
    """没有 SSE 或 GET 观察者也产生最终通知责任。"""
    result = await admitted(setup)
    for _ in range(3):
        await tick(setup)
    return result


def due(repository):
    """推进测试时钟到已知可重试任务的下一次领取，保留原发送时间与窗口。"""
    with repository.sessions.begin() as session:
        for row in session.scalars(select(Delivery).where(Delivery.due_at.is_not(None))):
            row.due_at = now() - timedelta(seconds=1)


def delivery(repository):
    """读取单条分片快照；该只读操作不会推进任何任务。"""
    with repository.sessions() as session:
        return session.scalar(select(Delivery))


@pytest.mark.asyncio
async def test_final_intent_atomic_and_independent_of_display(setup):
    """展示退出、重推原消息及审计发布都不会丢失或重复最终投递。"""
    service, message, run_id, repository = await completed(setup)
    assert await service.process(message, NoDisplay()) == "accepted"
    with setup.store.sessions() as session:
        event = session.scalar(select(Event))
        assert event.payload["content"] == "final answer"
        assert session.scalar(select(func.count()).select_from(Target)) == 1
        assert session.scalar(select(func.count()).select_from(Event)) == 1
        assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 2
    assert repository.materialize()
    assert not repository.materialize()
    first = delivery(repository)
    gateway = Gateway()
    claim = repository.claim("sender-after-bff-exit", lease_seconds=60)
    await deliver(repository, gateway, claim, setup.settings)
    final = delivery(repository)
    assert final.status == "sent" and final.message_id
    assert (first.content, first.send_key) == (final.content, final.send_key)
    assert gateway.calls[0]["address"]["message_id"] == message.message_id
    assert setup.backend.calls == 1
    assert repository.claim("other", lease_seconds=60) is None


@pytest.mark.asyncio
async def test_notification_failure_rolls_back_journal_and_completion(setup, monkeypatch):
    """通知意图写入后事务异常，Journal 与 completed 一起回滚；恢复不重跑模型。"""
    from financeclaw.shared.notifications import facts

    _, _, run_id, _ = await admitted(setup)
    await tick(setup)
    original = facts.record_progress

    def fail_after_intent(session, root):
        """在通知写入与提交之间注入崩溃。"""
        original(session, root)
        if root.projection["status"] == "completed":
            session.flush()
            raise RuntimeError("synthetic transaction failure")

    monkeypatch.setattr(facts, "record_progress", fail_after_intent)
    with pytest.raises(RuntimeError, match="synthetic transaction"):
        await tick(setup)
    with setup.store.sessions() as session:
        assert session.get(CoordinatedRunRow, run_id).projection["status"] != "completed"
        assert session.scalar(select(func.count()).select_from(Event)) == 0
        assert session.scalar(select(func.count()).select_from(ConversationMessageRow)) == 1
    monkeypatch.setattr(facts, "record_progress", original)
    await tick(setup)
    assert setup.backend.calls == 1
    with setup.store.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 1


@pytest.mark.asyncio
async def test_clear_retry_keeps_key_content_and_failure_limit(setup):
    """明确限流可恢复原投递；失败次数上限不触碰 Agent。"""
    _, _, _, repository = await completed(setup)
    repository.materialize()
    gateway = Gateway(receipts=[Receipt("retry", error_class="rate_limited")])
    await deliver(
        repository, gateway, repository.claim("sender1", lease_seconds=60), setup.settings
    )
    assert delivery(repository).status == "retry"
    due(repository)
    await deliver(
        repository, gateway, repository.claim("sender2", lease_seconds=60), setup.settings
    )
    assert delivery(repository).status == "sent"
    assert len({(call["send_key"], call["content"]) for call in gateway.calls}) == 1
    assert setup.backend.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("verified_window", [0, 120])
async def test_receipt_loss_recovers_only_with_verified_window(setup, verified_window):
    """未验收窗口时未知结果停留；已验收窗口内只用原键恢复。"""
    _, _, _, repository = await completed(setup)
    settings = setup.settings.model_copy(
        update={
            "notification_verified_dedup_seconds": verified_window,
            "notification_dedup_evidence": "synthetic-dedup-fixture",
        }
    )
    repository.materialize()
    gateway = Gateway(lose=True)
    await deliver(repository, gateway, repository.claim("first", lease_seconds=60), settings)
    assert delivery(repository).status == "uncertain"
    due(repository)
    claim = repository.claim("restarted", lease_seconds=60)
    if verified_window:
        await deliver(repository, gateway, claim, settings)
        assert delivery(repository).status == "sent"
        assert gateway.calls[0]["send_key"] == gateway.calls[1]["send_key"]
    else:
        assert claim is None
    assert len(gateway.messages) == 1
    assert setup.backend.calls == 1


@pytest.mark.asyncio
async def test_crashed_sender_fencing_and_expired_window(setup):
    """在途进程退出后按未知处理；旧回执不能覆盖接管 epoch，过窗不重发。"""
    _, _, _, repository = await completed(setup)
    repository.materialize()
    old = repository.claim("old", lease_seconds=60)
    assert repository.prepare(
        old, recovery_seconds=120, timeout_seconds=10, recovery_evidence="synthetic-dedup-fixture"
    )
    with repository.sessions.begin() as session:
        row = session.get(Delivery, old["delivery_id"])
        row.lease_until = now() - timedelta(seconds=1)
        row.recover_until = now() - timedelta(seconds=1)
    current = repository.claim("new", lease_seconds=60)
    with pytest.raises(StaleSender):
        repository.settle(old, Receipt("sent", message_id="late"), max_failures=5)
    gateway = Gateway()
    await deliver(repository, gateway, current, setup.settings)
    assert delivery(repository).status == "uncertain"
    assert delivery(repository).message_id is None
    assert not gateway.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "chat", "user", "allowlist"])
async def test_revoked_or_changed_target_never_receives_final(setup, change):
    """目的地撤销或重绑定，保存 suppressed 结果，不切换其他收件人或发送方式。"""
    _, _, run_id, repository = await completed(setup)
    if change == "revoke":
        setup.bff.runs.notifications(
            run_id, tenant_id="feishu:tenant", subject_id="feishu:user", revoke=True
        )
    elif change == "allowlist":
        repository.allowed_open_ids = frozenset()
    else:
        with repository.sessions.begin() as session:
            binding = session.scalar(select(ChannelConversationBindingRow))
            if change == "chat":
                binding.external_chat_id = "different-chat"
            else:
                binding.external_user_id = "different-user"
    repository.materialize()
    gateway = Gateway()
    await deliver(repository, gateway, repository.claim("sender", lease_seconds=60), setup.settings)
    assert delivery(repository).status == "suppressed"
    assert not gateway.calls


@pytest.mark.asyncio
async def test_answer_reuses_subscription_and_suppresses_old_interaction(setup):
    """决定消息不另订阅最终回复；已回答的旧提示在发送前被抑制。"""
    setup.backend.delegate = True
    service, message, run_id, repository = await admitted(setup)
    for _ in range(8):
        await tick(setup)
    with setup.store.sessions() as session:
        interaction = session.scalar(select(PendingInteractionRow))
        identifier, revision = interaction.interaction_id, interaction.revision
    repository.materialize()
    assert (
        await service.process(
            replace(
                message,
                message_id="answer-message",
                text=f'/answer {identifier} {revision} {{"scope": "all"}}',
            ),
            NoDisplay(),
        )
        == "accepted"
    )
    for _ in range(8):
        await tick(setup)
    while repository.materialize():
        pass
    gateway = Gateway()
    for _ in range(4):
        claim = repository.claim("sender", lease_seconds=60)
        if claim:
            await deliver(repository, gateway, claim, setup.settings)
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Target)) == 1
        assert {row.status for row in session.scalars(select(Delivery))} == {"suppressed", "sent"}
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["address"]["message_id"] == "original"


@pytest.mark.asyncio
async def test_chunks_are_ordered_and_uncertain_blocks_later_parts(setup):
    """每片有固定 UUID；前片未知时不能越过它，重启不再切分或补发首片。"""
    from financeclaw.bff.notifications.rendering import chunks

    assert "".join(part.split("\n", 1)[1] for part in chunks("数据🧪" * 1500)) == "数据🧪" * 1500
    _, _, _, repository = await completed(setup)
    with repository.sessions.begin() as session:
        event = session.scalar(select(Event))
        event.payload = {**event.payload, "content": "数据🧪" * 1500}
    repository.materialize()
    gateway = Gateway(lose=True)
    await deliver(repository, gateway, repository.claim("sender", lease_seconds=60), setup.settings)
    assert repository.claim("restart", lease_seconds=60) is None
    with repository.sessions() as session:
        rows = list(session.scalars(select(Delivery).order_by(Delivery.part)))
        assert len(rows) > 1 and len({row.send_key for row in rows}) == len(rows)
        assert rows[0].status == "uncertain" and all(row.status == "pending" for row in rows[1:])


@pytest.mark.asyncio
async def test_concurrent_sender_claims_one_original_delivery(setup):
    """真实 SQL 竞争只领取一个分片；进程级 PostgreSQL 验证另由隔离实验执行。"""
    _, _, _, repository = await completed(setup)
    repository.materialize()
    if setup.store.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("row claim concurrency requires PostgreSQL")
    claims = await asyncio.gather(
        *[asyncio.to_thread(repository.claim, f"sender-{i}", lease_seconds=60) for i in range(8)]
    )
    assert len([claim for claim in claims if claim]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("obsolete", ["expired", "cancelled"])
async def test_expired_or_cancelled_interaction_is_not_prompted(setup, obsolete):
    """即使原事件未消费或 Coordinator 尚未处理过期，也不能发送旧决定请求。"""
    setup.backend.delegate = True
    _, _, run_id, repository = await admitted(setup)
    for _ in range(8):
        await tick(setup)
    if obsolete == "expired":
        with repository.sessions.begin() as session:
            session.scalar(select(PendingInteractionRow)).expires_at = now() - timedelta(seconds=1)
    else:
        await setup.bff.cancel(run_id, tenant_id="feishu:tenant", subject_id="feishu:user")
    repository.materialize()
    gateway = Gateway()
    await deliver(repository, gateway, repository.claim("sender", lease_seconds=60), setup.settings)
    assert delivery(repository).status == "suppressed" and not gateway.calls


@pytest.mark.asyncio
async def test_notification_dead_letter_and_frozen_content_guard(setup):
    """明确失败耗尽后保留死信；冻结内容损坏时拒绝发送，也不恢复 Agent。"""
    _, _, _, repository = await completed(setup)
    repository.materialize()
    gateway = Gateway(receipts=[Receipt("retry", error_class="rate_limited")] * 2)
    settings = setup.settings.model_copy(update={"notification_max_failures": 2})
    for _ in range(2):
        due(repository)
        await deliver(repository, gateway, repository.claim("sender", lease_seconds=60), settings)
    assert delivery(repository).status == "dead_letter"
    assert repository.claim("other", lease_seconds=60) is None
    assert setup.backend.calls == 1


@pytest.mark.asyncio
async def test_frozen_content_mismatch_fails_before_network(setup):
    """不信任被意外改写的投递内容，错误不能扩大为发送另一个答案。"""
    _, _, _, repository = await completed(setup)
    repository.materialize()
    claim = repository.claim("sender", lease_seconds=60)
    with repository.sessions.begin() as session:
        session.get(Delivery, claim["delivery_id"]).content = "unexpected replacement"
    gateway = Gateway()
    await deliver(repository, gateway, claim, setup.settings)
    assert delivery(repository).status == "dead_letter" and not gateway.calls


@pytest.mark.asyncio
async def test_new_driver_is_fenced_from_old_worker_and_can_read_8a_roots(setup):
    """新根不会被不写通知的 8A Worker 领取；8B 能继续原协议的无订阅旧根。"""
    from tests.stage8.test_coordinator import admit

    _, _, run_id, _ = await admitted(setup)
    with setup.store.sessions() as session:
        assert session.get(CoordinatedRunRow, run_id).driver_version == 2
        assert (
            session.scalar(select(CoordinatedRunRow).where(CoordinatedRunRow.driver_version == 1))
            is None
        )
    for _ in range(3):
        await tick(setup)
    _, legacy = await admit(setup)
    with setup.store.sessions.begin() as session:
        session.get(CoordinatedRunRow, legacy.run_id).driver_version = 1
    for _ in range(3):
        await tick(setup)
    with setup.store.sessions() as session:
        row = session.get(CoordinatedRunRow, legacy.run_id)
        assert row.driver_version == 1 and row.projection["status"] == "completed"
        assert session.scalar(select(func.count()).select_from(Target)) == 1
