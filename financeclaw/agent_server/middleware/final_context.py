"""每次实际模型尝试前的容量检查与 Manifest；不再读取历史 Journal。"""

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from hashlib import sha256
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from financeclaw.agent_server.context.planning import completed_tool_batches
from financeclaw.agent_server.context.turns import trusted_context
from financeclaw.shared.conversation.models import ManifestMemoryReference, ModelContextManifest
from financeclaw.shared.llm.budget import (
    ContextBudget,
    ContextBudgetPlanner,
    TokenCounter,
    request_payload,
)
from financeclaw.shared.turns.types import ExecutionConflict

MEMORY_REFS_KEY = "financeclaw_memory_refs"
logger = logging.getLogger(__name__)


class RequestRecorder:
    """同步执行的薄记录器，供回答与原生摘要的实际模型调用共同使用。"""

    def __init__(
        self, budget: ContextBudget, repository=None, *, profile_version="unknown", planner=None
    ):
        """保存容量策略及可选的业务 Manifest 仓储。"""
        self.budget = budget
        self.repository = repository
        self.profile_version = profile_version
        self.counter = TokenCounter()
        self.planner = planner

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
        state=None,
    ):
        """完整输入超过硬容量时停止调用；正常请求只保存来源和 hash。"""
        model_name = str(
            getattr(model, "model_name", getattr(model, "model", type(model).__name__))
        )
        provider = str(getattr(model, "_llm_type", type(model).__name__))
        schemas = [convert_to_openai_tool(tool) for tool in tools]
        payload = request_payload(
            messages, tools=tools, output_schema=output_schema, model_settings=model_settings
        )
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        actual = ContextBudgetPlanner.from_model(
            model, self.budget, reserve_application_output=subtype != "summary"
        )
        planner = self.planner if subtype == "answer" and self.planner else actual
        tokens = planner.check(
            messages, tools=tools, output_schema=output_schema, model_settings=model_settings
        )
        actual.check(
            messages, tools=tools, output_schema=output_schema, model_settings=model_settings
        )
        limit = min(planner.input_limit, actual.input_limit)
        if self.repository is None or context.conversation_id is None:
            return None
        references = {}
        artifacts = set()
        summaries = []
        omissions = []
        for message in messages:
            omissions.extend(message.additional_kwargs.get("financeclaw_memory_omissions", ()))
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
            prompt_template_version=f"native-thread/{self.profile_version}",
            agent_profile_version=self.profile_version,
            model_profile_version=(getattr(model, "metadata", None) or {})
            .get("financeclaw_model_profile", {})
            .get("version", "unregistered"),
            provider=provider,
            model=model_name,
            subtype=subtype,
            token_count_method=planner.estimator_id,
            memory_owner_revision=(state or {}).get("memory_recall", {}).get("owner_revision"),
            privacy_epoch=(state or {}).get("context_privacy_epoch"),
            working_summary_version=((state or {}).get("working_context") or {}).get(
                "summary_version"
            ),
            compaction_reason=(state or {}).get("context_compaction_reason"),
            message_ids=tuple(message.id for message in messages if message.id),
            summary_sources=tuple(summaries),
            memory_ids=tuple(references),
            memory_refs=tuple(references.values()),
            omissions=tuple(omissions),
            tool_result_refs=tuple(sorted(artifacts)),
            exposed_tools=tuple(
                schema.get("function", {}).get("name", "unknown") for schema in schemas
            ),
            input_token_count=tokens,
            available_input_tokens=limit,
            context_hash=sha256(canonical.encode()).hexdigest(),
        )
        return self.repository.save_manifest(manifest)

    def observe(self, manifest, response):
        """记录Provider实际用量；模型无usage时保留空值，回填故障不重跑成功调用。"""
        if manifest is None or self.repository is None:
            return
        results = getattr(response, "result", None)
        message = results[0] if results else response
        usage = getattr(message, "usage_metadata", None) or {}
        if not usage:
            metadata = getattr(message, "response_metadata", {}) or {}
            usage = metadata.get("token_usage", {})
        inputs = usage.get("input_tokens", usage.get("prompt_tokens"))
        outputs = usage.get("output_tokens", usage.get("completion_tokens"))
        if inputs is None and outputs is None:
            return
        try:
            self.repository.record_manifest_usage(
                manifest.manifest_id, input_tokens=inputs, output_tokens=outputs
            )
        except Exception as exc:
            logger.warning(
                "model_usage_record_failed manifest_id=%s error_type=%s",
                manifest.manifest_id,
                type(exc).__name__,
            )


class FinalContextMiddleware(AgentMiddleware):
    """放在重试、fallback 和输入转换内部，每个真实尝试记录一次。"""

    def __init__(self, recorder: RequestRecorder, *, privacy_epoch_reader=None):
        """保存容量策略及可选的业务 Manifest 仓储。"""
        self.recorder = recorder
        self.privacy_epoch_reader = privacy_epoch_reader

    def _record(self, request):
        """记录完成全部输入变换后的实际模型请求。"""
        if completed_tool_batches(request.messages) is None:
            raise ExecutionConflict("model request contains an incomplete tool batch")
        if self.privacy_epoch_reader is not None:
            expected = request.state.get("context_privacy_epoch")
            if expected is not None:
                current = self.privacy_epoch_reader(trusted_context(request.runtime))
                if current != expected:
                    raise ValueError(
                        "privacy epoch changed before actual model request; prepare again"
                    )
        return self.recorder.record(
            trusted_context(request.runtime),
            request.model,
            [*([request.system_message] if request.system_message else []), *request.messages],
            tools=request.tools,
            output_schema=request.response_format,
            model_settings=request.model_settings,
            state=request.state,
        )

    def wrap_model_call(self, request, handler: Callable):
        """检查并记录同步请求。"""
        manifest = self._record(request)
        response = handler(request)
        self.recorder.observe(manifest, response)
        return response

    async def awrap_model_call(self, request, handler: Callable):
        """检查并记录异步请求，数据库 I/O 不占用事件循环。"""
        manifest = await asyncio.to_thread(self._record, request)
        response = await handler(request)
        await asyncio.to_thread(self.recorder.observe, manifest, response)
        return response
