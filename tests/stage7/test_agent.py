"""真实 LangGraph＋真实本地引擎＋离线模型，验证可执行闭环而不只检查配置。"""

import pytest
from pydantic import ValidationError

from financeclaw.agent_server.agents.ziwei_offline import OfflineZiweiModel
from financeclaw.agent_server.domains.ziwei.errors import ZiweiError
from financeclaw.kernel.ziwei import BirthTime, TargetSelector, ZiweiTextResult
from financeclaw.shared.infrastructure.settings import FinanceClawSettings
from tests.stage7.support import build_ziwei_agent, components, context, envelope, request, settings
from tests.support import build_components


def test_default_disabled_root_and_explicit_root_allowlist():
    """开关两侧都使用根 1.5.0，只有启用后才允许紫微子图。"""
    base = build_components(
        FinanceClawSettings(
            _env_file=None, environment="test", offline_model=True, debug_full_io=False
        )
    )
    assert base.default_agent_profile.version == "1.5.0"
    assert not any("ziwei" in ref.tool_id for ref in base.default_agent_profile.allowed_tools)
    active = components()
    assert active.default_agent_profile.version == "1.5.0"
    for stack in (base, active):
        with pytest.raises(LookupError):
            stack.agent_profiles.resolve("finance_agent", "1.2.0")
        assert [key for key in stack.agent_profiles if key[0] == "finance_agent"] == [
            ("finance_agent", "1.5.0")
        ]
    names = {ref.tool_id for ref in active.default_agent_profile.allowed_tools}
    assert {name for name in names if "ziwei" in name} == {"call_agent__ziwei_doushu_agent"}
    specialist = active.agent_profiles.resolve("ziwei_doushu_agent")
    assert len(specialist.allowed_tools) == 1 and not specialist.interaction_points
    for ref in specialist.allowed_tools:
        tool = active.tool_catalog.resolve(ref.tool_id, ref.version)
        assert "runtime" not in tool.tool.tool_call_schema.model_json_schema()["properties"]
        assert {"birth", "target", "level", "focus", "mode"} <= set(
            tool.tool.tool_call_schema.model_json_schema()["properties"]
        )


@pytest.mark.parametrize(
    ("changes", "expected_error"),
    [
        ({"ziwei_convention": None}, "explicit ziwei_convention"),
        ({"ziwei_hmac_key": "short"}, "ziwei_hmac_key must contain at least 32 bytes"),
        ({"debug_full_io": True}, "Ziwei requires debug_full_io=false"),
        ({"langsmith_hide_inputs": False}, "Ziwei requires debug_full_io=false"),
        ({"langsmith_hide_outputs": False}, "Ziwei requires debug_full_io=false"),
        ({"environment": "staging"}, "Ziwei candidate is restricted to development/test"),
    ],
)
def test_candidate_configuration_fails_closed(changes, expected_error, monkeypatch):
    """规则、密钥、原文保护与环境未明确配置时不允许开启。"""
    # 验证未显式放行的默认行为，避免本地调试环境变量改变用例语义。
    monkeypatch.delenv("FINANCECLAW_ZIWEI_ALLOW_FULL_IO", raising=False)
    with pytest.raises(ValidationError) as error:
        settings(**changes)
    # 只检查校验器消息，避免入参回显中的字段名误命中错误断言。
    assert expected_error in error.value.errors()[0]["msg"]


@pytest.mark.parametrize("environment", ["development", "test"])
def test_candidate_full_io_can_be_explicitly_enabled(environment, monkeypatch):
    """环境变量只放行日志保护校验，不覆盖用户的具体 tracing 设置。"""
    monkeypatch.setenv("FINANCECLAW_ZIWEI_ALLOW_FULL_IO", "true")
    configured = settings(
        environment=environment,
        debug_full_io=True,
        langsmith_hide_inputs=False,
        langsmith_hide_outputs=False,
    )
    assert configured.ziwei_allow_full_io
    assert configured.debug_full_io
    assert not configured.langsmith_hide_inputs
    assert not configured.langsmith_hide_outputs


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_candidate_full_io_override_is_restricted_to_local_environments(environment):
    """即使紫微未启用，正式环境也不能保留完整 I/O 放行开关。"""
    with pytest.raises(ValidationError, match="ziwei_allow_full_io is restricted"):
        settings(environment=environment, ziwei_enabled=False, ziwei_allow_full_io=True)


@pytest.mark.parametrize("changes", [{"ziwei_convention": None}, {"ziwei_hmac_key": "short"}])
def test_candidate_full_io_override_keeps_other_validation(changes):
    """放行日志校验不绕过规则与密钥校验。"""
    with pytest.raises(ValidationError):
        settings(ziwei_allow_full_io=True, **changes)


def test_irrelevant_calendar_and_fold_parameters_cannot_be_silently_ignored():
    """只用于农历或钟表歧义的参数不能用于无关的输入类型。"""
    with pytest.raises(ValidationError):
        TargetSelector(kind="calendar_period", unit="year", year=2026, is_leap_month=True)
    with pytest.raises(ValidationError):
        BirthTime(kind="shichen", shichen="yin", fold=1)


@pytest.mark.parametrize("mode", ["chart_only", "interpretation"])
@pytest.mark.asyncio
async def test_real_graph_produces_validated_evidence_and_result(mode):
    """子 Agent 一次取日盘，最终结构化结果保留实际盘面与证据。"""
    stack = components()
    profile = stack.agent_profiles.resolve("ziwei_doushu_agent", "2.1.0")
    graph = build_ziwei_agent(
        stack.agent_factory, profile, stack.ziwei_service, model=OfflineZiweiModel()
    )
    result = await graph.ainvoke(envelope(request(mode=mode)), context=context())
    value = profile.output_schema.model_validate(result["ziwei_result"])
    assert value.outcome == ("answer" if mode == "interpretation" else "chart_only")
    assert len(value.charts_used) == 1 and value.charts_used[0].level == "daily"
    assert sum(m.type == "tool" for m in result["messages"]) == 1
    assert result["ziwei_model_calls"] == (3 if mode == "interpretation" else 2)
    if mode == "interpretation":
        assert value.answer_text and value.schema_version == 2
        assert "interpretations" not in result["ziwei_result"]


@pytest.mark.asyncio
async def test_tool_clarifies_after_one_function_call():
    """缺资料正常结束，根会话据此发问；不创建 Agent-child interrupt。"""
    stack = components()
    profile = stack.agent_profiles.resolve("ziwei_doushu_agent")
    graph = build_ziwei_agent(
        stack.agent_factory, profile, stack.ziwei_service, model=OfflineZiweiModel()
    )
    result = await graph.ainvoke(envelope(request(birth={})), context=context())
    value = ZiweiTextResult.model_validate(result["ziwei_result"])
    assert value.outcome == "needs_clarification" and "birth.date" in value.missing_fields
    assert result["ziwei_model_calls"] == 1 and not result.get("ziwei_evidence")
    assert sum(m.type == "tool" for m in result["messages"]) == 1


@pytest.mark.asyncio
async def test_initialize_permissions_and_input_state_cannot_be_forged():
    """外部 graph 输入不能注入伪造盘面；缺权限在任何计算前拒绝。"""
    stack = components()
    graph = build_ziwei_agent(
        stack.agent_factory,
        stack.agent_profiles.resolve("ziwei_doushu_agent"),
        stack.ziwei_service,
        model=OfflineZiweiModel(),
    )
    with pytest.raises(PermissionError):
        await graph.ainvoke(envelope(request()), context=context(scopes=set()))
    with pytest.raises(PermissionError):
        await graph.ainvoke(envelope(request()), context=context(data_classification="internal"))
    data = {
        **envelope(request()),
        "ziwei_evidence": [{"tool_call_id": "forged"}],
        "ziwei_result": {"outcome": "answer"},
    }
    result = await graph.ainvoke(data, context=context())
    assert result["ziwei_result"]["charts_used"][0]["chart_id"] != "forged"


@pytest.mark.asyncio
async def test_too_small_prompt_budget_fails_instead_of_truncating():
    """工具字节阈值以外，还检查最终模型上下文的整体预算。"""
    stack = components()
    graph = build_ziwei_agent(
        stack.agent_factory,
        stack.agent_profiles.resolve("ziwei_doushu_agent"),
        stack.ziwei_service,
        model=OfflineZiweiModel(),
        input_budget=128,
    )
    with pytest.raises(ZiweiError) as error:
        await graph.ainvoke(envelope(request(mode="interpretation")), context=context())
    assert error.value.code == "ZIWEI_CONTEXT_BUDGET_EXCEEDED"
