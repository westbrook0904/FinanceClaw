"""文本解读热修复：自由正文、可信外壳、预算与冻结发布兼容。"""

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ValidationError

from financeclaw.modules.ziwei.errors import ZiweiError
from financeclaw.modules.ziwei.models import ZiweiAgentResult, ZiweiTextResult
from financeclaw.orchestration.agents.release import configuration_fingerprint
from financeclaw.orchestration.agents.ziwei_offline import OfflineZiweiModel
from financeclaw.orchestration.graphs.ziwei_agent import (
    ZiweiEvidenceMiddleware,
    build_ziwei_agent,
    interpretation_text,
)
from tests.stage7.support import components, context, envelope, request


class TextResponseModel(OfflineZiweiModel):
    """用普通响应覆盖一次解读调用；真实工具取证不变。"""

    final_response: AIMessage
    final_calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """禁止文本解读请求重新绑定 JSON mode 或温度。"""
        if not self._bound_tool_names:
            assert not kwargs.get("response_format")
            assert "temperature" not in kwargs
            assert "JSON Schema" not in messages[0].content
            self.final_calls += 1
            return ChatResult(generations=[ChatGeneration(message=self.final_response)])
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.parametrize(
    "text",
    [
        "## 事业\n\n从传统解释看可关注沟通节奏，不代表必然发生。",
        "没有固定字段，也没有逐条 fact_id。",
        '{"outcome":"unsupported","charts_used":["forged"]',
        "文字长于旧字段约束也不触发格式修复：" + "a" * 2100,
    ],
)
@pytest.mark.asyncio
async def test_free_text_is_delivered_without_json_parsing_or_repair(text):
    """JSON 风格正文只是文本，不能覆盖程序生成的 outcome／盘面身份。"""
    stack = components()
    model = TextResponseModel(final_response=AIMessage(content=text))
    profile = stack.agent_profiles.resolve("ziwei_doushu_agent")
    result = await build_ziwei_agent(
        stack.agent_factory, profile, stack.ziwei_service, model=model
    ).ainvoke(envelope(request(mode="interpretation")), context=context())
    value = ZiweiTextResult.model_validate(result["ziwei_result"])
    assert value.answer_text == text
    assert value.outcome == "answer" and value.schema_version == 2
    assert value.charts_used[0].chart_id != "forged"
    assert "interpretations" not in result["ziwei_result"]
    assert "evidence_refs" not in result["ziwei_result"]
    assert model.final_calls == 1 and result["ziwei_model_calls"] == 3
    assert stack.model_profiles.resolve(profile.model_profile).temperature == 0


@pytest.mark.parametrize(
    "response,code",
    [
        (AIMessage(content=" \n\t"), "ZIWEI_INTERPRETATION_EMPTY"),
        (
            AIMessage(content="", additional_kwargs={"reasoning_content": "private thinking"}),
            "ZIWEI_INTERPRETATION_EMPTY",
        ),
        (
            AIMessage(content="未完成的解读", response_metadata={"finish_reason": "length"}),
            "ZIWEI_INTERPRETATION_INCOMPLETE",
        ),
        (
            AIMessage(content="拦截", response_metadata={"finish_reason": "content_filter"}),
            "ZIWEI_INTERPRETATION_INCOMPLETE",
        ),
        (
            AIMessage(
                content="失败", response_metadata={"finish_reason": "insufficient_system_resource"}
            ),
            "ZIWEI_INTERPRETATION_INCOMPLETE",
        ),
        (
            AIMessage(
                content="不应调用工具",
                tool_calls=[
                    {
                        "id": "unexpected",
                        "name": "ziwei_natal_chart",
                        "args": {},
                        "type": "tool_call",
                    }
                ],
            ),
            "ZIWEI_INTERPRETATION_INCOMPLETE",
        ),
    ],
)
@pytest.mark.asyncio
async def test_incomplete_or_empty_output_fails_without_format_retry(response, code):
    """去除业务表达 Schema 不会把空输出、截断或工具调用冒充完整解读。"""
    stack = components()
    model = TextResponseModel(final_response=response)
    graph = build_ziwei_agent(
        stack.agent_factory,
        stack.agent_profiles.resolve("ziwei_doushu_agent"),
        stack.ziwei_service,
        model=model,
    )
    with pytest.raises(ZiweiError) as error:
        await graph.ainvoke(envelope(request(mode="interpretation")), context=context())
    assert error.value.code == code
    assert model.final_calls == 1


def test_content_blocks_only_extract_visible_text():
    """兼容文本块，但不把思考、工具或任意非文本块转成解读。"""
    response = AIMessage(
        content=[
            {"type": "reasoning", "text": "private reasoning"},
            {"type": "text", "text": "## 解读\n"},
            "正文",
            {"type": "output_text", "text": "仅供参考。"},
        ]
    )
    assert interpretation_text(response) == "## 解读\n正文仅供参考。"
    with pytest.raises(ZiweiError, match="正文"):
        interpretation_text(AIMessage(content=[{"type": "reasoning", "text": "private"}]))


def test_v2_envelope_still_rejects_missing_charts_or_wrong_protocol():
    """父委派边界校验的是协议与业务状态，不是算命答案对不对。"""
    with pytest.raises(ValidationError, match="calculated charts"):
        ZiweiTextResult(outcome="answer", answer_text="凭空算好的盘")
    with pytest.raises(ValidationError):
        ZiweiTextResult(
            schema_version=1, outcome="unsupported", warnings=("未启用",), error_code="x"
        )
    with pytest.raises(ValidationError, match="interpretation text"):
        ZiweiTextResult(
            outcome="needs_clarification",
            question="日期？",
            missing_fields=("date",),
            answer_text="资料不全仍然强行解读",
        )
    with pytest.raises(ValidationError, match="calculated charts"):
        ZiweiTextResult(outcome="chart_only", charts_used=())


def test_evidence_budget_does_not_expand_when_json_repair_is_removed():
    """新发布最多 6 次取证＋1 次文本，旧发布仍为 6＋2。"""
    stack = components()
    for version, reserve in [("1.0.0", 2), ("2.0.0", 1)]:
        profile = stack.agent_profiles.resolve("ziwei_doushu_agent", version)
        middleware = ZiweiEvidenceMiddleware(
            max_calls=profile.max_model_calls, input_budget=24_000, finalization_calls=reserve
        )
        assert middleware.before_model({"ziwei_model_calls": 5}, None)["ziwei_model_calls"] == 6
        with pytest.raises(ZiweiError, match="取证调用预算"):
            middleware.before_model({"ziwei_model_calls": 6}, None)


def test_legacy_release_fingerprints_and_output_schema_are_unchanged():
    """基线为热修复前同一组合根的合成配置，旧检查点不能静默绑定新行为。"""
    stack = components()
    expected = {
        (
            "finance_agent",
            "1.3.0",
        ): "e4afd7d8c0a97e5deb55046c790d1380b0fc80969a607d5b09a5c8710aabab2e",
        (
            "ziwei_doushu_agent",
            "1.0.0",
        ): "7bdfa02b4d63fe55ed40974adbf863579b905ae0b4628845e71cf0fb8904bfde",
    }
    for key, fingerprint in expected.items():
        assert configuration_fingerprint(stack.agent_profiles.resolve(*key)) == fingerprint
    assert configuration_fingerprint(ZiweiAgentResult.model_json_schema()) == (
        "f87091cfa1fd794fd514dcf80dde4cd1d49fab7ac3365ce9dbe650b901786dbe"
    )
    assert (
        stack.agent_profiles.resolve("ziwei_doushu_agent", "1.0.0").output_schema
        is ZiweiAgentResult
    )
    assert (
        stack.agent_profiles.resolve("ziwei_doushu_agent", "2.0.0").output_schema is ZiweiTextResult
    )
