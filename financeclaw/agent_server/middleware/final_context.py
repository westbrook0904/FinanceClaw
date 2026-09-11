"""每次实际模型尝试前的容量检查与 Manifest；不再读取历史 Journal。"""

import asyncio
import json
from collections.abc import Callable, Sequence
from hashlib import sha256
from typing import Any
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from financeclaw.agent_server.context.budget import ContextBudget, TokenCounter
from financeclaw.agent_server.context.turns import trusted_context
from financeclaw.shared.conversation.models import ManifestMemoryReference, ModelContextManifest

MEMORY_REFS_KEY = "financeclaw_memory_refs"


def response_schema(value: Any) -> Any:
    """取出结构化输出 schema，计入请求容量和指纹。"""
    schema = getattr(value, "schema", value)
    if schema is None or isinstance(schema, dict):
        return schema
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    spec = getattr(value, "schema_spec", None)
    return getattr(spec, "json_schema", str(schema))


class RequestRecorder:
    """同步执行的薄记录器，供回答与原生摘要的实际模型调用共同使用。"""

    def __init__(self, budget: ContextBudget, repository=None, *, profile_version="unknown"):
        """保存容量策略及可选的业务 Manifest 仓储。"""
        self.budget = budget
        self.repository = repository
        self.profile_version = profile_version
        self.counter = TokenCounter()

    def record(
        self,
        context,
        model,
        messages: Sequence[BaseMessage],
        *,
        tools=(),
        output_schema=None,
        subtype="answer",
        model_settings=None,
    ):
        """完整输入超过硬容量时停止调用；正常请求只保存来源和 hash。"""
        model_name = str(
            getattr(model, "model_name", getattr(model, "model", type(model).__name__))
        )
        provider = str(getattr(model, "_llm_type", type(model).__name__))
        schemas = [convert_to_openai_tool(tool) for tool in tools]
        settings = {
            name: getattr(model, name)
            for name in ("max_tokens", "temperature", "top_p", "seed")
            if getattr(model, name, None) is not None
        }
        settings.update(model_settings or {})
        payload = {
            "messages": [message.model_dump(mode="json") for message in messages],
            "tools": schemas,
            "response_format": response_schema(output_schema),
            "model": model_name,
            "provider": provider,
            "settings": settings,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        tokens = self.counter.text(canonical)
        profile = getattr(model, "profile", None) or {}
        capacity = profile.get("max_input_tokens") or self.budget.model_input_limit
        limit = min(self.budget.model_input_limit, capacity) - (
            self.budget.reserved_output_tokens + self.budget.safety_margin
        )
        if tokens > limit:
            raise ValueError(f"mandatory model context exceeds input budget: {tokens} > {limit}")
        if self.repository is None or context.conversation_id is None:
            return None
        references = {}
        artifacts = set()
        summaries = []
        for message in messages:
            for reference in message.additional_kwargs.get(MEMORY_REFS_KEY, []):
                item = ManifestMemoryReference.model_validate(reference)
                references[item.memory_id] = item
            reference = message.additional_kwargs.get("artifact_ref")
            if isinstance(reference, dict) and reference.get("artifact_id"):
                artifacts.add(reference["artifact_id"])
            if message.additional_kwargs.get("lc_source") == "summarization":
                summaries.append(message.additional_kwargs.get("summary_source", {}))
        manifest = ModelContextManifest(
            manifest_id=f"manifest-{uuid4().hex}",
            model_call_id=f"model-call-{uuid4().hex}",
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            run_id=context.run_id,
            prompt_template_version=f"native-thread/{self.profile_version}",
            agent_profile_version=self.profile_version,
            model_profile_version=(getattr(model, "metadata", None) or {})
            .get("financeclaw_model_profile", {})
            .get("version", "unregistered"),
            provider=provider,
            model=model_name,
            subtype=subtype,
            message_ids=tuple(message.id for message in messages if message.id),
            summary_sources=tuple(summaries),
            memory_ids=tuple(references),
            memory_refs=tuple(references.values()),
            tool_result_refs=tuple(sorted(artifacts)),
            exposed_tools=tuple(
                schema.get("function", {}).get("name", "unknown") for schema in schemas
            ),
            input_token_count=tokens,
            available_input_tokens=limit,
            context_hash=sha256(canonical.encode()).hexdigest(),
        )
        return self.repository.save_manifest(manifest)


class FinalContextMiddleware(AgentMiddleware):
    """放在重试、fallback 和输入转换内部，每个真实尝试记录一次。"""

    def __init__(self, recorder: RequestRecorder):
        """保存容量策略及可选的业务 Manifest 仓储。"""
        self.recorder = recorder

    def _record(self, request):
        """记录完成全部输入变换后的实际模型请求。"""
        self.recorder.record(
            trusted_context(request.runtime),
            request.model,
            [*([request.system_message] if request.system_message else []), *request.messages],
            tools=request.tools,
            output_schema=request.response_format,
            model_settings=request.model_settings,
        )

    def wrap_model_call(self, request, handler: Callable):
        """检查并记录同步请求。"""
        self._record(request)
        return handler(request)

    async def awrap_model_call(self, request, handler: Callable):
        """检查并记录异步请求，数据库 I/O 不占用事件循环。"""
        await asyncio.to_thread(self._record, request)
        return await handler(request)
