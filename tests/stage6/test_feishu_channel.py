"""Current channel and resource boundary regressions."""

from types import SimpleNamespace

import pytest
from lark_channel import Conversation, Identity, InboundMessage, TextContent
from pydantic import SecretStr, ValidationError

from financeclaw.bff.channels.feishu import FeishuChannelAdapter
from financeclaw.shared.infrastructure.settings import FinanceClawSettings


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
