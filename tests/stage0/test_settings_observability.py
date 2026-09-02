import logging

import pytest
from pydantic import ValidationError

from financeclaw_spike.context import SpikeContext
from financeclaw_spike.graph import create_demo_agent
from financeclaw_spike.observability import FullPromptDebugMiddleware, redact_sensitive
from financeclaw_spike.settings import SpikeSettings


def test_production_rejects_full_io_debug() -> None:
    with pytest.raises(ValidationError, match="debug_full_io"):
        SpikeSettings(environment="production", debug_full_io=True)


def test_redaction_masks_secrets_but_keeps_trace_shape() -> None:
    value = {
        "authorization": "Bearer secret-value",
        "nested": [
            "use sk-abcdefghijklmnopqrstuvwxyz1234",
            "postgresql://user:password@localhost/db",
        ],
        "safe": "AAPL",
    }

    assert redact_sensitive(value) == {
        "authorization": "<redacted>",
        "nested": ["use <redacted>", "<redacted>"],
        "safe": "AAPL",
    }


def test_debug_middleware_logs_redacted_payload(caplog: pytest.LogCaptureFixture) -> None:
    middleware = FullPromptDebugMiddleware(enabled=True)
    caplog.set_level(logging.DEBUG, logger="financeclaw_spike.model_io")

    middleware._log("request", {"api_key": "secret", "prompt": "read AAPL"})

    assert "read AAPL" in caplog.text
    assert "secret" not in caplog.text
    assert "<redacted>" in caplog.text


def test_agent_debug_logs_final_prompt_schema_model_and_tool_io(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="financeclaw_spike.model_io")
    agent = create_demo_agent(
        settings=SpikeSettings(
            environment="test",
            offline_model=True,
            debug_full_io=True,
        )
    )

    agent.invoke(
        {"messages": [{"role": "user", "content": "read AAPL"}]},
        config={"configurable": {"thread_id": "stage0-debug-io"}},
        context=SpikeContext(request_id="debug-io", environment="test"),
    )

    assert "model_request=" in caplog.text
    assert "read_market_snapshot" in caplog.text
    assert "model_response=" in caplog.text
    assert "model_tool_request=" in caplog.text
    assert "model_tool_response=" in caplog.text
