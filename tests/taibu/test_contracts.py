"""输入与时间口径的必要约束，避免默认值和远端配置覆盖发布边界。"""

import pytest
from pydantic import ValidationError

from financeclaw.agent_server.tools.taibu import remote_arguments
from financeclaw.kernel.taibu import TaibuAlmanacInput, TaibuBaziInput, TaibuError
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from tests.taibu.conftest import BAZI


@pytest.mark.parametrize(
    "change",
    [
        {"birth_minute": None},
        {"birth_hour": True},
        {"birth_day": 1.5},
        {"birth_month": 2, "birth_day": 30},
        {"birth_year": 1800},
        {"calendar_type": "lunar"},
        {"calendar_type": "lunar", "is_leap_month": False, "birth_day": 31},
        {"is_leap_month": True},
        {"time_basis": "local_clock"},
        {"time_basis": "true_solar"},
        {"longitude": 116.4},
        {"solar_time": "true_solar"},
        {"solar_time": "true_solar", "longitude": True},
        {"solar_time": "true_solar", "longitude": "116.4"},
        {"url": "https://example.com"},
    ],
)
def test_invalid_birth_fields_never_reach_remote(change):
    """拒绝不明确或冲突输入，含布尔数值和额外连接字段。"""
    with pytest.raises(ValidationError):
        TaibuBaziInput.model_validate({**BAZI, **change})


def test_lunar_input_and_single_solar_correction(stack):
    """农历闰月与经度精确映射，不在客户端预先校正出生时间。"""
    value = TaibuBaziInput.model_validate(
        {
            **BAZI,
            "calendar_type": "lunar",
            "is_leap_month": True,
            "solar_time": "true_solar",
            "longitude": 116.4,
        }
    )
    arguments, convention = remote_arguments(value, stack.context)
    assert arguments["birthHour"] == 9 and arguments["birthMinute"] == 0
    assert arguments["isLeapMonth"] is True and arguments["longitude"] == 116.4
    assert "birthPlace" not in arguments
    assert convention["solar_time"] == "true_solar"


@pytest.mark.parametrize(
    "arguments",
    [{}, {"date": "2026-02-30"}, {"date": "2026-09-12", "day_offset": 0}, {"day_offset": True}],
)
def test_almanac_requires_exactly_one_real_target(arguments):
    """拒绝缺失日期和混合表示。"""
    with pytest.raises(ValidationError):
        TaibuAlmanacInput.model_validate(arguments)


def test_relative_date_uses_frozen_request_clock(stack):
    """重放时以旧请求午夜前的时钟解析明天，不读取运行机器的今天。"""
    value = TaibuAlmanacInput(day_offset=1)
    for _ in range(2):
        assert remote_arguments(value, stack.context)[0] == {"date": "2026-09-12"}
    with pytest.raises(TaibuError, match="TAIBU_CLOCK_REQUIRED"):
        remote_arguments(value, stack.context.model_copy(update={"request_clock": None}))


@pytest.mark.parametrize(
    "change",
    [
        {"taibu_mcp_url": "https://evil.example/mcp"},
        {"taibu_mcp_url": "http://u:p@taibu-mcp:3001/mcp"},
        {"taibu_mcp_url": "http://taibu-mcp:3001/mcp?token=secret"},
        {"taibu_allowed_tools": []},
        {"taibu_allowed_tools": ["tarot"]},
        {"debug_full_io": True},
        {"langsmith_hide_inputs": False},
        {"artifact_inline_bytes": 2048},
        {
            "taibu_mcp_url": "https://MCP.MINGAI.FUN./mcp",
            "taibu_allowed_hosts": ["mcp.mingai.fun"],
        },
        {
            "taibu_egress": "external",
            "taibu_mcp_url": "https://mcp.mingai.fun/mcp",
            "taibu_allowed_hosts": ["mcp.mingai.fun"],
        },
    ],
)
def test_configuration_cannot_silently_expand_egress(settings, change):
    """错误路由、敏感调试和越界能力在装配前即被拒绝。"""
    with pytest.raises(ValueError):
        FinanceClawSettings(_env_file=None, **{**settings.model_dump(), **change})
