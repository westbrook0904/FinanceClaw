"""Stage 6 飞书 P2P 入站、会话绑定、幂等、串行与降级测试。"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from langchain_core.messages import AIMessage
from lark_channel import Conversation, Identity, InboundMessage, TextContent
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, inspect

from financeclaw.application import (
    ConversationService,
    FeishuChannelService,
    FeishuInboundMessage,
    ServerRun,
)
from financeclaw.bootstrap import build_components
from financeclaw.infrastructure import FinanceClawSettings
from financeclaw.interfaces.channels import FeishuChannelAdapter
from financeclaw.modules.conversation import SqlAlchemyConversationRepository


class _FakeAgentClient:
    """可流式完成会话 run 并记录并发与顺序的 Agent Server 替身。"""

    def __init__(self, *, delay: float = 0.0) -> None:
        """初始化运行记录与可控流延迟。"""
        self.delay = delay
        self.runs: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.stream_order: list[str] = []
        self.active_streams = 0
        self.max_active_streams = 0

    async def create_thread(self, thread_id: str) -> None:
        """幂等创建线程；替身无需持久化。"""
        del thread_id

    async def create_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        input: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ServerRun:
        """登记运行，并为最终查询准备确定性助手回复。"""
        run_id = f"server-{len(self.runs) + 1}"
        message = str(input["messages"][0]["content"])
        self.create_calls.append(
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "assistant_id": assistant_id,
                "message": message,
                "context": context,
                "metadata": metadata,
            }
        )
        self.runs[run_id] = {
            "run_id": run_id,
            "thread_id": thread_id,
            "status": "pending",
            "metadata": metadata,
            "message": message,
            "output": {"messages": [AIMessage(content=f"答复：{message}")]},
        }
        return ServerRun(run_id=run_id, status="pending")

    async def get_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """返回运行快照。"""
        assert self.runs[run_id]["thread_id"] == thread_id
        return self.runs[run_id]

    async def join_run(self, *, thread_id: str, run_id: str) -> Mapping[str, Any]:
        """返回运行的最终线程状态。"""
        assert self.runs[run_id]["thread_id"] == thread_id
        return self.runs[run_id]["output"]

    async def find_run(self, *, thread_id: str, application_run_id: str) -> ServerRun | None:
        """按业务运行 ID 查找已经创建的 server run。"""
        for run_id, run in self.runs.items():
            if (
                run["thread_id"] == thread_id
                and run["metadata"].get("application_run_id") == application_run_id
            ):
                return ServerRun(run_id=run_id, status=str(run["status"]))
        return None

    async def resume_run(
        self,
        *,
        thread_id: str,
        assistant_id: str,
        command: dict[str, Any],
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> Mapping[str, Any]:
        """提供完整 Port；本测试不会进入审批恢复。"""
        del thread_id, assistant_id, command, context, metadata
        return {"messages": [AIMessage(content="恢复完成")]}

    def stream_run(self, *, thread_id: str, run_id: str) -> AsyncIterator[Any]:
        """返回指定运行的文本增量流，并在结束前置成功状态。"""
        assert self.runs[run_id]["thread_id"] == thread_id

        async def parts() -> AsyncIterator[Any]:
            """记录并发，产出进度和两个助手增量。"""
            message = str(self.runs[run_id]["message"])
            self.active_streams += 1
            self.max_active_streams = max(self.max_active_streams, self.active_streams)
            self.stream_order.append(f"start:{message}")
            try:
                yield {"event": "updates", "data": {"private": "hidden"}}
                yield {
                    "event": "messages",
                    "data": [{"type": "AIMessageChunk", "content": "答复："}, {}],
                }
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield {
                    "event": "messages",
                    "data": [{"type": "AIMessageChunk", "content": message}, {}],
                }
                self.runs[run_id]["status"] = "success"
            finally:
                self.active_streams -= 1
                self.stream_order.append(f"end:{message}")

        return parts()

    async def health(self) -> bool:
        """报告替身可用。"""
        return True


class _MarkdownStream:
    """记录 append/set_content 后最终可见文本的卡片流替身。"""

    def __init__(self) -> None:
        """初始化空文本与更新记录。"""
        self.content = ""
        self.updates: list[str] = []

    async def append(self, chunk: str) -> None:
        """追加文本并记录快照。"""
        self.content += chunk
        self.updates.append(self.content)

    async def set_content(self, full: str) -> None:
        """覆盖文本并记录快照。"""
        self.content = full
        self.updates.append(self.content)


class _FakeGateway:
    """执行 producer 并记录卡片、文本降级的飞书网关替身。"""

    def __init__(self, *, fail_after_stream: bool = False) -> None:
        """配置是否在 producer 完成后模拟 CardKit 收尾失败。"""
        self.fail_after_stream = fail_after_stream
        self.streams: list[tuple[str, str, _MarkdownStream]] = []
        self.texts: list[dict[str, str]] = []

    async def stream_markdown(
        self,
        *,
        chat_id: str,
        reply_to_message_id: str,
        producer,
    ) -> bool:
        """执行流生产者并保存最终卡片内容。"""
        stream = _MarkdownStream()
        self.streams.append((chat_id, reply_to_message_id, stream))
        await producer(stream)
        if self.fail_after_stream:
            raise RuntimeError("simulated CardKit finish failure")
        return True

    async def send_text(
        self,
        *,
        chat_id: str,
        reply_to_message_id: str,
        text: str,
        idempotency_key: str,
    ) -> bool:
        """记录普通文本降级消息。"""
        self.texts.append(
            {
                "chat_id": chat_id,
                "reply_to": reply_to_message_id,
                "text": text,
                "idempotency_key": idempotency_key,
            }
        )
        return True


def _stack(tmp_path: Path, *, delay: float = 0.0):
    """构建使用 SQLite Journal 的飞书应用服务测试栈。"""
    components = build_components(
        FinanceClawSettings(
            environment="test",
            offline_model=True,
            debug_full_io=False,
            database_url=SecretStr(f"sqlite+pysqlite:///{tmp_path / 'stage6.db'}"),
            artifact_root=str(tmp_path / "artifacts"),
        ),
        enable_persistence=True,
    )
    repository = components.conversation_repository
    assert isinstance(repository, SqlAlchemyConversationRepository)
    client = _FakeAgentClient(delay=delay)
    conversations = ConversationService(client, repository, components.agent_profiles)
    service = FeishuChannelService(
        conversations,
        app_id="cli_test",
        allowed_open_ids=frozenset({"ou_a", "ou_b"}),
        scopes=frozenset({"market:read", "tools:read"}),
        max_concurrency=2,
        status_poll_interval_seconds=0,
        status_timeout_seconds=1,
    )
    return components, repository, client, service


def _message(
    message_id: str,
    text: str,
    *,
    user: str = "ou_a",
    tenant: str = "tenant_a",
    chat: str = "oc_a",
    content_type: str = "text",
    chat_type: str = "p2p",
    sender_type: str | None = "user",
    sender_is_bot: bool = False,
) -> FeishuInboundMessage:
    """创建一条确定性的应用层飞书入站消息。"""
    return FeishuInboundMessage(
        message_id=message_id,
        tenant_key=tenant,
        sender_open_id=user,
        chat_id=chat,
        chat_type=chat_type,
        content_type=content_type,
        text=text,
        sender_type=sender_type,
        sender_is_bot=sender_is_bot,
    )


@pytest.mark.asyncio
async def test_p2p_binding_reuse_message_idempotency_and_final_correction(tmp_path: Path) -> None:
    """验证同一单聊复用会话、重复消息不执行两次且卡片等于 Journal。"""
    components, repository, client, service = _stack(tmp_path)
    gateway = _FakeGateway()
    inbound = _message("om_1", "  查询 AAPL  ")

    assert await service.process(inbound, gateway) == "completed"
    assert await service.process(inbound, gateway) == "completed"

    binding = repository.get_channel_binding(
        channel="feishu",
        app_id="cli_test",
        tenant_key="tenant_a",
        external_chat_id="oc_a",
    )
    assert binding is not None
    assert len(client.create_calls) == 1
    conversation = repository.get_owned(
        binding.conversation_id,
        "feishu:tenant_a",
        "feishu:ou_a",
    )
    messages = repository.list_messages(conversation.conversation_id)
    assert [(item.role.value, item.content) for item in messages] == [
        ("user", "查询 AAPL"),
        ("assistant", "答复：查询 AAPL"),
    ]
    assert gateway.streams[-1][2].content == messages[-1].content
    if components.database is not None:
        components.database.close()


@pytest.mark.asyncio
async def test_same_chat_is_serial_and_different_chats_are_isolated(tmp_path: Path) -> None:
    """验证同 chat 顺序稳定，而不同用户可并行且不会串会话身份。"""
    components, repository, client, service = _stack(tmp_path, delay=0.03)
    gateway = _FakeGateway()

    await asyncio.gather(
        service.process(_message("om_1", "第一条"), gateway),
        service.process(_message("om_2", "第二条"), gateway),
    )
    assert client.stream_order == [
        "start:第一条",
        "end:第一条",
        "start:第二条",
        "end:第二条",
    ]

    client.stream_order.clear()
    client.max_active_streams = 0
    await asyncio.gather(
        service.process(_message("om_3", "用户 A", chat="oc_a"), gateway),
        service.process(
            _message("om_4", "用户 B", user="ou_b", tenant="tenant_b", chat="oc_b"),
            gateway,
        ),
    )
    assert client.max_active_streams == 2
    first = repository.get_channel_binding(
        channel="feishu",
        app_id="cli_test",
        tenant_key="tenant_a",
        external_chat_id="oc_a",
    )
    second = repository.get_channel_binding(
        channel="feishu",
        app_id="cli_test",
        tenant_key="tenant_b",
        external_chat_id="oc_b",
    )
    assert first is not None and second is not None
    assert first.conversation_id != second.conversation_id
    if components.database is not None:
        components.database.close()


@pytest.mark.asyncio
async def test_filtering_non_text_reply_and_card_fallback(tmp_path: Path) -> None:
    """验证群聊/机器人被丢弃、非文本可见提示及 CardKit 文本降级。"""
    components, _, client, service = _stack(tmp_path)
    gateway = _FakeGateway()
    assert await service.process(_message("om_g", "群聊", chat_type="group"), gateway) == "ignored"
    assert (
        await service.process(_message("om_bot", "回声", sender_is_bot=True), gateway) == "ignored"
    )
    assert (
        await service.process(_message("om_system", "系统", sender_type="system"), gateway)
        == "ignored"
    )
    assert await service.process(_message("om_unknown", "越权", user="ou_x"), gateway) == "ignored"
    assert (
        await service.process(_message("om_img", "", content_type="image"), gateway)
        == "unsupported"
    )
    assert gateway.texts[-1]["text"] == FeishuChannelService.UNSUPPORTED_TEXT
    assert not client.create_calls

    fallback = _FakeGateway(fail_after_stream=True)
    assert await service.process(_message("om_text", "需要兜底"), fallback) == "completed"
    assert fallback.texts[-1]["text"] == "答复：需要兜底"
    assert len(fallback.texts[-1]["idempotency_key"]) == 36
    if components.database is not None:
        components.database.close()


def test_adapter_extracts_tenant_only_from_verified_raw_event() -> None:
    """验证真实 SDK 嵌套消息使用同消息已验证事件信封补齐 tenant_key。"""
    adapter = FeishuChannelAdapter(
        SimpleNamespace(),  # type: ignore[arg-type]
        app_id="cli_test",
        app_secret="secret",
        allowed_open_ids=frozenset({"ou_a"}),
        max_concurrency=1,
    )
    adapter._capture_raw_event(
        {
            "header": {"tenant_key": "tenant_a"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_a"}},
                "message": {"message_id": "om_1", "content": "must-not-be-kept"},
            },
        }
    )
    message = InboundMessage(
        id="om_1",
        create_time=1,
        sender=Identity(open_id="ou_a", sender_type="user"),
        conversation=Conversation(chat_id="oc_a", chat_type="p2p"),
        content=TextContent(text="hello"),
        raw_content_type="text",
        body_text="hello",
    )

    normalized = adapter.normalize_message(message)

    assert normalized.tenant_key == "tenant_a"
    assert normalized.sender_open_id == "ou_a"
    assert normalized.chat_id == "oc_a"
    assert normalized.chat_type == "p2p"
    assert normalized.sender_type == "user"
    assert not normalized.sender_is_bot
    assert adapter._tenant_by_message == {}
    assert not hasattr(normalized, "raw")


def test_adapter_builds_official_sdk_with_p2p_only_policy() -> None:
    """验证锁定 SDK 能接受一期配置，且只开放白名单单聊与脱敏事件。"""
    adapter = FeishuChannelAdapter(
        SimpleNamespace(),  # type: ignore[arg-type]
        app_id="cli_test",
        app_secret="secret",
        allowed_open_ids=frozenset({"ou_a"}),
        max_concurrency=2,
        security_mode="audit",
    )

    channel = adapter._build_channel()
    policy = channel.get_policy()

    assert policy.dm_policy == "allowlist"
    assert policy.group_policy == "disabled"
    assert policy.allow_from == ["ou_a"]
    assert channel._config.inbound.include_raw is False
    assert channel._config.inbound.emit_raw_events is True
    assert channel._config.safety.chat_queue.enabled is False
    assert channel._config.security.mode == "audit"
    assert channel._config.security.max_concurrent_ws_handlers == 2


def test_feishu_settings_fail_closed_and_hide_secret() -> None:
    """验证 Channel 默认关闭，开启时强制凭证、白名单和显式 scope。"""
    disabled = FinanceClawSettings(environment="test", offline_model=True, debug_full_io=False)
    assert not disabled.feishu_enabled
    with pytest.raises(ValidationError):
        FinanceClawSettings(
            environment="test",
            offline_model=True,
            debug_full_io=False,
            feishu_enabled=True,
        )
    enabled = FinanceClawSettings(
        environment="test",
        offline_model=True,
        debug_full_io=False,
        feishu_enabled=True,
        feishu_app_id="cli_test",
        feishu_app_secret=SecretStr("top-secret"),
        feishu_allowed_open_ids=frozenset({"ou_a"}),
    )
    assert "top-secret" not in repr(enabled)


def test_stage6_migration_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证绑定表可升级、回滚到 stage5 后再升级。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("FINANCECLAW_ENVIRONMENT", "test")
    monkeypatch.setenv("FINANCECLAW_DATABASE_URL", url)
    config = Config("alembic.ini")

    command.upgrade(config, "head")
    engine = create_engine(url)
    assert "channel_conversation_bindings" in inspect(engine).get_table_names()
    engine.dispose()

    command.downgrade(config, "0005_stage5")
    engine = create_engine(url)
    assert "channel_conversation_bindings" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(url)
    assert "channel_conversation_bindings" in inspect(engine).get_table_names()
    engine.dispose()
