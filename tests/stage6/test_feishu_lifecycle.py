"""飞书 SDK 首次导入不得捕获 ASGI 主循环。"""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("loop_kind", ["asyncio", "uvloop"])
@pytest.mark.parametrize("fail_connect", [False, True])
def test_cold_sdk_import_keeps_application_loop_alive(loop_kind: str, fail_connect: bool) -> None:
    """独立进程覆盖冷导入、工作线程运行 WS 循环及连接失败清理。"""
    # 其他测试在收集阶段导入 SDK，会掩盖真实 BFF 在 lifespan 首次导入的问题。
    script = r"""
import asyncio
import sys
from types import SimpleNamespace
from financeclaw.integrations.feishu.channel import FeishuChannelAdapter

assert "lark_channel.ws.client" not in sys.modules

async def shutdown():
    pass

async def main():
    application_loop = asyncio.get_running_loop()
    adapter = FeishuChannelAdapter(
        SimpleNamespace(shutdown=shutdown), app_id="cli_test", app_secret="test",
        allowed_open_ids=frozenset({"ou_test"}), max_concurrency=1,
    )
    build = adapter._build_channel
    observed = []
    def build_probe():
        channel = build()
        from lark_channel.ws import client
        async def connect_until_ready(*, timeout):
            assert client.loop is not application_loop, "SDK captured the ASGI event loop"
            async def tick():
                return asyncio.get_running_loop() is client.loop
            assert await asyncio.to_thread(lambda: client.loop.run_until_complete(tick()))
            if sys.argv[2] == "True":
                raise RuntimeError("synthetic transport failure")
            channel._ready_flag = True
        disconnect = channel.disconnect
        async def disconnect_probe():
            await disconnect()
            observed.append("disconnected")
        channel.connect_until_ready = connect_until_ready
        channel.disconnect = disconnect_probe
        return channel
    adapter._build_channel = build_probe
    try:
        await adapter.start()
    except RuntimeError as exc:
        assert sys.argv[2] == "True"
        assert str(exc) == "synthetic transport failure"
    else:
        assert sys.argv[2] == "False"
        assert await adapter.health()
    await adapter.stop()
    await asyncio.sleep(0)
    assert application_loop.is_running()
    assert observed == ["disconnected"]
    from lark_channel.ws import client
    client.loop.close()

if sys.argv[1] == "uvloop":
    import uvloop
    uvloop.run(main())
else:
    asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, loop_kind, str(fail_connect)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
