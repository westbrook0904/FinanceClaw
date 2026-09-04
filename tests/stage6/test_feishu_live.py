"""Stage 6 飞书真实凭证 WebSocket 连接探针。"""

import os
from typing import Any

import pytest

from financeclaw.interfaces.channels import FeishuChannelAdapter

_APP_ID = os.getenv("FINANCECLAW_FEISHU_E2E_APP_ID")
_APP_SECRET = os.getenv("FINANCECLAW_FEISHU_E2E_APP_SECRET")
_OPEN_ID = os.getenv("FINANCECLAW_FEISHU_E2E_OPEN_ID")


class _ProbeService:
    """连接探针使用的无业务副作用服务。"""

    def submit(self, message: Any, gateway: Any) -> None:
        """忽略探针期间偶然到达的消息，不创建 Conversation。"""
        del message, gateway

    async def shutdown(self) -> None:
        """探针没有后台业务任务需要排空。"""


@pytest.mark.external
@pytest.mark.skipif(
    not all((_APP_ID, _APP_SECRET, _OPEN_ID)),
    reason="Feishu E2E app credentials and canary open_id are not configured",
)
@pytest.mark.asyncio
async def test_live_feishu_websocket_connects_and_becomes_ready() -> None:
    """使用显式 E2E 凭证验证官方 SDK WebSocket 可连接并进入 ready。"""
    assert _APP_ID is not None and _APP_SECRET is not None and _OPEN_ID is not None
    adapter = FeishuChannelAdapter(
        _ProbeService(),  # type: ignore[arg-type]
        app_id=_APP_ID,
        app_secret=_APP_SECRET,
        allowed_open_ids=frozenset({_OPEN_ID}),
        max_concurrency=1,
        security_mode="audit",
        connect_timeout_seconds=30,
    )
    try:
        await adapter.start()
        assert await adapter.health()
    finally:
        await adapter.stop()
