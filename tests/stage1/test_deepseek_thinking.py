"""验证非思考配置经过真实 SDK 序列化，覆盖无推理字段的澄清恢复消息。"""

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import SecretStr

from financeclaw.agent_server.llm import factory as factory_module
from financeclaw.agent_server.llm.factory import ModelFactory
from financeclaw.kernel.models import ModelProfile, ModelProfileCatalog, ModelProfileRef
from financeclaw.shared.releases.interactions import CLARIFICATION_TOOL


def _factory(model_name: str) -> ModelFactory:
    """使用虚拟端点创建真实模型工厂，不接触外部服务或业务资料。"""
    profile = ModelProfile(profile_id="test", version="1.0.0", model=model_name)
    return ModelFactory(
        ModelProfileCatalog((profile,)),
        api_key=SecretStr("test-placeholder"),
        base_url="https://example.invalid",
    )


def test_other_models_do_not_receive_deepseek_options() -> None:
    """非 DeepSeek 模型仍使用原有参数。"""
    model = _factory("openai:gpt-4o-mini").create(
        ModelProfileRef(profile_id="test", version="1.0.0")
    )
    assert model.extra_body is None
    assert "thinking" not in model._get_request_payload([HumanMessage(content="test")])


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["invoke", "ainvoke", "stream", "astream"])
async def test_clarification_requests_disable_thinking_on_wire(monkeypatch, method: str) -> None:
    """同步、异步和流式请求都将 thinking 放入 HTTP JSON 顶层。"""
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        """检查 SDK 发出的真实 JSON，并返回不含推理字段的模拟响应。"""
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["thinking"] == {"type": "disabled"}
        assert "extra_body" not in payload
        assert payload["tools"][0]["function"]["name"] == CLARIFICATION_TOOL
        assert "reasoning_content" not in payload["messages"][1]
        assert payload["messages"][2]["content"] == "当地钟表时间"
        response = {
            "id": "test-response",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "finish_reason": "stop"}],
        }
        message = {"role": "assistant", "content": "已收到补充资料"}
        if payload.get("stream"):
            response["object"] = "chat.completion.chunk"
            response["choices"][0]["delta"] = message
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(response)}\n\ndata: [DONE]\n\n",
            )
        response["choices"][0]["message"] = message
        return httpx.Response(200, json=response)

    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as sync_client:
        async with httpx.AsyncClient(transport=transport) as async_client:
            initialize = factory_module.init_chat_model

            def init_with_transport(model_name, **kwargs):
                """保留工厂的模型参数，仅替换 HTTP 传输以避免外部请求。"""
                return initialize(
                    model_name, http_client=sync_client, http_async_client=async_client, **kwargs
                )

            monkeypatch.setattr(factory_module, "init_chat_model", init_with_transport)
            model = _factory("openai:deepseek-v4-flash").create(
                ModelProfileRef(profile_id="test", version="1.0.0")
            )
            bound = model.bind_tools(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": CLARIFICATION_TOOL,
                            "description": "补充缺失资料",
                            "parameters": {
                                "type": "object",
                                "properties": {"question": {"type": "string"}},
                            },
                        },
                    }
                ]
            )
            messages = [
                HumanMessage(content="合成样例"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": CLARIFICATION_TOOL,
                            "args": {"question": "出生记录所用时制？"},
                            "id": "test-clarification",
                        }
                    ],
                ),
                ToolMessage(content="当地钟表时间", tool_call_id="test-clarification"),
            ]
            if method == "invoke":
                content = bound.invoke(messages).content
            elif method == "ainvoke":
                content = (await bound.ainvoke(messages)).content
            elif method == "stream":
                content = "".join(chunk.content for chunk in bound.stream(messages))
            else:
                content = "".join([chunk.content async for chunk in bound.astream(messages)])
            assert content == "已收到补充资料"
    assert len(requests) == 1
