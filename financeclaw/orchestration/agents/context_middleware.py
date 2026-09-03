"""在每次模型调用前装配并记录可复现的会话上下文。"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langsmith import traceable, tracing_context

from financeclaw.kernel import ExecutionContext
from financeclaw.modules.conversation.context import ConversationContextBuilder
from financeclaw.modules.conversation.models import ManifestMemoryReference, ModelContextManifest
from financeclaw.modules.conversation.repository import ConversationRepository
from financeclaw.orchestration.tools import ToolCatalog

from .memory_middleware import MEMORY_REFS_KEY
from .middleware import redact_sensitive

LOGGER = logging.getLogger("financeclaw.model_io")


@traceable(name="conversation.recall", run_type="retriever", tags=["stage:2"])
def trace_conversation_recall(
    *, conversation_id: str, recent_count: int, summary_count: int, historical_count: int
) -> None:
    """记录会话上下文候选检索的数量与选择结果。"""
    del conversation_id, recent_count, summary_count, historical_count


@traceable(name="context.manifest.persist", run_type="chain", tags=["stage:2"])
def trace_manifest_persist(*, model_call_id: str, context_hash: str) -> None:
    """记录上下文清单的持久化结果与关联标识。"""
    del model_call_id, context_hash


class ConversationContextMiddleware(AgentMiddleware):
    """在模型调用前替换系统上下文，并持久化本次选择清单。

    适用场景：
        用于 Agent 模型或工具调用进入下一处理器前后的横切治理场景。

    属性：
        builder: 按运行配置创建图、上下文或其他复杂对象的构建器。
        repository: 负责领域状态读写和事务一致性的仓储。
        tool_catalog: 登记并解析所有可用受治理工具版本的目录。
        agent_profile_version: 本次运行固定使用的 Agent 配置版本。
        model_profile_version: 本次模型调用固定使用的模型配置版本。
        prompt_template_version: 构造模型提示时使用的模板版本。
        debug_full_io: 是否记录脱敏后的完整模型与工具输入输出；生产环境默认关闭。
    """

    def __init__(
        self,
        *,
        builder: ConversationContextBuilder,
        repository: ConversationRepository,
        tool_catalog: ToolCatalog,
        agent_profile_version: str,
        model_profile_version: str,
        prompt_template_version: str,
        debug_full_io: bool,
    ) -> None:
        """注入并保存会话上下文Middleware所需的协作对象，同时校验构造期不变量。"""
        super().__init__()
        self.builder = builder
        self.repository = repository
        self.tool_catalog = tool_catalog
        self.agent_profile_version = agent_profile_version
        self.model_profile_version = model_profile_version
        self.prompt_template_version = prompt_template_version
        self.debug_full_io = debug_full_io

    @staticmethod
    def _context(request: ModelRequest) -> ExecutionContext:
        """从 LangChain 请求或工具运行时提取并校验可信执行上下文。"""
        value = request.runtime.context
        if isinstance(value, ExecutionContext):
            return value
        if isinstance(value, Mapping):
            return ExecutionContext.model_validate(value)
        raise TypeError("trusted ExecutionContext is required")

    def _prepare(self, request: ModelRequest) -> tuple[ModelRequest, ModelContextManifest | None]:
        """构建模型消息、选择证据与清单，并用新上下文替换请求内容。"""
        context = self._context(request)
        if context.conversation_id is None:
            return request, None
        system_prompt = ""
        if request.system_message is not None:
            content = request.system_message.content
            system_prompt = (
                content if isinstance(content, str) else json.dumps(content, default=str)
            )
        tools = list(request.tools)
        memory_references: tuple[ManifestMemoryReference, ...] = ()
        if request.system_message is not None:
            raw_references = request.system_message.additional_kwargs.get(MEMORY_REFS_KEY, ())
            if isinstance(raw_references, list | tuple):
                memory_references = tuple(
                    ManifestMemoryReference.model_validate(item) for item in raw_references
                )
        messages, selection = self.builder.build(
            context=context,
            runtime_messages=request.messages,
            system_prompt=system_prompt,
            tools=tools,
            memory_references=memory_references,
        )
        trace_conversation_recall(
            conversation_id=context.conversation_id,
            recent_count=len(selection.recent_message_ids),
            summary_count=len(selection.summary_ids),
            historical_count=len(selection.historical_message_ids),
        )
        journal_messages = {
            item.message_id: item for item in self.repository.list_messages(context.conversation_id)
        }
        recent_sequences = [
            journal_messages[item_id].sequence
            for item_id in selection.recent_message_ids
            if item_id in journal_messages
        ]
        exposed_tools: list[str] = []
        for tool in tools:
            tool_id = str(getattr(tool, "name", "unknown"))
            try:
                managed = self.tool_catalog.resolve(tool_id)
                exposed_tools.append(f"{managed.governance.tool_id}@{managed.governance.version}")
            except LookupError:
                exposed_tools.append(tool_id)
        model_call_id = f"model-call-{uuid4().hex}"
        manifest = ModelContextManifest(
            manifest_id=f"manifest-{uuid4().hex}",
            model_call_id=model_call_id,
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            run_id=context.run_id,
            prompt_template_version=self.prompt_template_version,
            agent_profile_version=self.agent_profile_version,
            model_profile_version=self.model_profile_version,
            recent_message_start=min(recent_sequences) if recent_sequences else None,
            recent_message_end=max(recent_sequences) if recent_sequences else None,
            summary_ids=selection.summary_ids,
            memory_ids=tuple(item.memory_id for item in selection.memory_refs),
            memory_refs=selection.memory_refs,
            historical_message_ids=selection.historical_message_ids,
            tool_result_refs=selection.tool_result_refs,
            exposed_tools=tuple(exposed_tools),
            input_token_count=selection.input_token_count,
            available_input_tokens=selection.available_input_tokens,
            omissions=selection.omissions,
            context_hash=selection.context_hash,
            created_at=datetime.now(UTC),
        )
        self.repository.save_manifest(manifest)
        trace_manifest_persist(
            model_call_id=manifest.model_call_id,
            context_hash=manifest.context_hash,
        )
        if self.debug_full_io:
            LOGGER.debug(
                "final_model_context=%s",
                json.dumps(
                    redact_sensitive(
                        {
                            **selection.debug_payload,
                            "model_call_id": model_call_id,
                            "manifest": manifest.model_dump(mode="json"),
                        }
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            )
        return request.override(messages=messages), manifest

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """在同步模型调用前后应用该中间件职责。"""
        prepared, manifest = self._prepare(request)
        metadata = (
            {"model_call_id": manifest.model_call_id, "context_hash": manifest.context_hash}
            if manifest is not None
            else {}
        )
        with tracing_context(metadata=metadata):
            return handler(prepared)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """在异步模型调用前后应用该中间件职责。"""
        prepared, manifest = await asyncio.to_thread(self._prepare, request)
        metadata = (
            {"model_call_id": manifest.model_call_id, "context_hash": manifest.context_hash}
            if manifest is not None
            else {}
        )
        with tracing_context(metadata=metadata):
            return await handler(prepared)
