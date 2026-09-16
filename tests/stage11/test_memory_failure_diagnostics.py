"""保留记忆模型截断的实际用量与首次失败，且不重置跨重试预算。"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from openai import LengthFinishReasonError
from openai.types.chat import ChatCompletion
from pydantic import SecretStr

from financeclaw.kernel.models import ModelProfile, ModelProfileCatalog, ModelProfileRef
from financeclaw.memory_worker.model import StructuredMemoryModel
from financeclaw.memory_worker.prompts import MemorySuggestions
from financeclaw.shared.llm import factory as factory_module
from financeclaw.shared.llm.factory import ModelFactory
from financeclaw.shared.llm.memory_profiles import memory_model_profiles, memory_profile_fingerprint
from financeclaw.shared.outbox.repository import ModelBudgetExhausted
from financeclaw.shared.outbox.tables import OutboxEventRow
from tests.stage1.test_model_configuration import CONFIG, configured
from tests.stage11.test_worker_outbox import claim
from tests.stage11.test_worker_outbox import worker_outbox as worker_outbox_fixture

worker_outbox = worker_outbox_fixture


@pytest.mark.asyncio
async def test_memory_qwen_disables_thinking_on_wire_without_changing_chat(tmp_path, monkeypatch):
    """真实 SDK 请求使用非思考结构化 JSON，聊天档案和原有输出上限保持独立。"""
    settings = configured(
        tmp_path,
        CONFIG.replace(
            'model = "openai:deepseek-synthetic-child"',
            'model = "openai:qwen-synthetic"\nenable_thinking = true',
        ),
    )
    extraction, consolidation = memory_model_profiles(settings)
    assert extraction.enable_thinking is False and consolidation.enable_thinking is False
    assert extraction.max_tokens == 2000 and consolidation.max_tokens == 4000
    original = next(
        p for p in settings.model_configuration.profiles() if p.profile_id == "child-main"
    )
    assert original.enable_thinking is True
    assert memory_profile_fingerprint(extraction) != memory_profile_fingerprint(
        extraction.model_copy(update={"enable_thinking": True})
    )
    requests = []

    def respond(request):
        """捕获实际 HTTP JSON，不访问供应商。"""
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["enable_thinking"] is False
        assert "extra_body" not in payload
        assert payload["response_format"]["type"] == "json_schema"
        assert payload.get("max_completion_tokens", payload.get("max_tokens")) == 2000
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 0,
                "model": "qwen-synthetic",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"suggestions":[]}'},
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        initialize = factory_module.init_chat_model
        monkeypatch.setattr(
            factory_module,
            "init_chat_model",
            lambda name, **kw: initialize(name, http_async_client=client, **kw),
        )
        factory = ModelFactory(
            ModelProfileCatalog((extraction,)),
            api_key=None,
            base_url=None,
            connections={
                extraction.connection_id: factory_module.ModelConnection(
                    SecretStr("synthetic-key"), "https://example.invalid/v1"
                )
            },
        )
        model = factory.create(
            ModelProfileRef(profile_id=extraction.profile_id, version=extraction.version)
        )
        parsed = await model.with_structured_output(MemorySuggestions).ainvoke("synthetic")
        assert parsed.suggestions == ()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_truncation_usage_and_first_failure_survive_budget_exhaustion(worker_outbox):
    """两次 SDK 截断后的第三次投递不再请求模型，诊断仅保存数字与异常名。"""
    database, outbox = worker_outbox
    completion = ChatCompletion.model_validate(
        {
            "id": "synthetic",
            "object": "chat.completion",
            "created": 0,
            "model": "synthetic",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "PRIVATE_PROVIDER_RESPONSE"},
                }
            ],
            "usage": {
                "prompt_tokens": 669,
                "completion_tokens": 2000,
                "total_tokens": 2669,
                "completion_tokens_details": {"reasoning_tokens": 2000},
            },
        }
    )

    class TruncatedModel:
        """模拟 SDK 在返回 raw 之前抛出截断异常。"""

        calls = 0

        def with_structured_output(self, *args, **kwargs):
            """沿用结构化适配器入口。"""
            return self

        async def ainvoke(self, messages):
            """每次调用都消耗预算且不能得到有效 JSON。"""
            self.calls += 1
            raise LengthFinishReasonError(completion=completion)

    provider = TruncatedModel()
    model = StructuredMemoryModel(
        provider,
        ModelProfile(
            profile_id="test",
            version="1.0.0",
            model="offline",
            max_tokens=2000,
            token_estimator="utf8-bytes-v1",
        ),
        outbox,
    )
    for expected in (LengthFinishReasonError, LengthFinishReasonError, ModelBudgetExhausted):
        event = claim(outbox)
        with pytest.raises(expected):
            await model.generate(event, "synthetic", {}, snapshot_id="test", max_attempts=2)
        outbox.mark_failed("job", expected.__name__, max_attempts=3, claim_epoch=event.claim_epoch)
        with database.session_factory.begin() as session:
            session.get(OutboxEventRow, "job").available_at = datetime.now(UTC) - timedelta(
                seconds=1
            )
    result = outbox.get("job")
    assert result.status == "dead_letter" and provider.calls == 2
    metadata = result.processing_metadata
    assert metadata["model_budget"]["attempts"] == 2
    assert (
        metadata["model_usage"]
        == [
            {
                "input_tokens": 669,
                "output_tokens": 2000,
                "total_tokens": 2669,
                "reasoning_tokens": 2000,
            }
        ]
        * 2
    )
    assert [item["error_type"] for item in metadata["failure_history"]] == [
        "LengthFinishReasonError",
        "LengthFinishReasonError",
        "ModelBudgetExhausted",
    ]
    assert "PRIVATE_PROVIDER_RESPONSE" not in json.dumps(metadata)
