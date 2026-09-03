"""会话上下文中间件：每次模型调用前按 token 预算组装最终上下文。

属于 orchestration.agents 的核心中间件：把 runtime 消息与会话日志中的最近原文、
摘要、古老历史按预算拼装为最终 Prompt 区域，并持久化 ModelContextManifest 供
审计与追溯；development 模式下保留完整 Prompt 明文便于调试。

"""

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

# 模型输入/输出调试日志器，development 模式下输出脱敏后的完整 Prompt。
LOGGER = logging.getLogger("financeclaw.model_io")


@traceable(name="conversation.recall", run_type="retriever", tags=["stage:2"])
def trace_conversation_recall(
    *, conversation_id: str, recent_count: int, summary_count: int, historical_count: int
) -> None:
    """上报本次上下文召回的观测 span（各类消息入选数量）。

    Args:
        conversation_id: 会话标识。
        recent_count: 入选的最近原文消息数量。
        summary_count: 入选的摘要数量。
        historical_count: 入选的古老历史消息数量。

    """
    del conversation_id, recent_count, summary_count, historical_count


@traceable(name="context.manifest.persist", run_type="chain", tags=["stage:2"])
def trace_manifest_persist(*, model_call_id: str, context_hash: str) -> None:
    """上报 Manifest 持久化的观测 span（模型调用标识与上下文哈希）。

    Args:
        model_call_id: 本次模型调用的唯一标识。
        context_hash: 最终上下文内容的哈希，用于跨调用比对。

    """
    del model_call_id, context_hash


class ConversationContextMiddleware(AgentMiddleware):
    """在每次模型调用前组装最终上下文并持久化 ModelContextManifest 的中间件。

    使用场景：由 AgentFactory 在会话仓储与上下文构建器就绪时挂到 Agent 上；
    每次模型调用前完成预算裁剪、消息拼装、Manifest 落库与 LangSmith 观测，
    并把 model_call_id 与 context_hash 写入追踪元数据。

    Attributes:
        builder: 会话上下文构建器，负责 token 预算计算与消息选取。
        repository: 会话仓储，用于读取日志消息与保存 Manifest。
        tool_catalog: 工具目录，用于解析暴露工具的治理标识与版本。
        agent_profile_version: 当前 Agent 档案版本，写入 Manifest 便于追溯。
        model_profile_version: 当前模型档案版本，写入 Manifest 便于追溯。
        prompt_template_version: 系统提示模板版本，格式为 ``<agent_id>-system/<version>``。
        debug_full_io: 是否在调试日志中输出脱敏后的完整最终上下文。

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
        """保存各依赖引用，供每次模型调用前的上下文组装使用。

        Args:
            builder: 会话上下文构建器。
            repository: 会话仓储。
            tool_catalog: 工具目录。
            agent_profile_version: Agent 档案版本。
            model_profile_version: 模型档案版本。
            prompt_template_version: 系统提示模板版本。
            debug_full_io: 是否输出完整输入输出调试日志。

        """
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
        """从模型调用请求中取出受信任的 ExecutionContext。

        Args:
            request: LangChain 中间件的模型调用请求。

        Returns:
            ExecutionContext: 运行时上下文；为映射时按模型校验后返回。

        Raises:
            TypeError: runtime.context 既非 ExecutionContext 也非映射时抛出。

        """
        value = request.runtime.context
        if isinstance(value, ExecutionContext):
            return value
        if isinstance(value, Mapping):
            return ExecutionContext.model_validate(value)
        raise TypeError("trusted ExecutionContext is required")

    def _prepare(self, request: ModelRequest) -> tuple[ModelRequest, ModelContextManifest | None]:
        """组装最终上下文消息并生成 Manifest，返回覆盖后的请求。"""
        # 1. 解析执行上下文；无 conversation_id 时无会话持久化，直接放行。
        context = self._context(request)
        if context.conversation_id is None:
            return request, None
        # 2. 序列化系统提示与工具列表，供预算计算与 Manifest 记录使用。
        system_prompt = ""
        if request.system_message is not None:
            content = request.system_message.content
            system_prompt = (
                content if isinstance(content, str) else json.dumps(content, default=str)
            )
        tools = list(request.tools)
        # 3. 从系统消息附加参数中提取记忆中间件注入的记忆引用。
        memory_references: tuple[ManifestMemoryReference, ...] = ()
        if request.system_message is not None:
            raw_references = request.system_message.additional_kwargs.get(MEMORY_REFS_KEY, ())
            if isinstance(raw_references, list | tuple):
                memory_references = tuple(
                    ManifestMemoryReference.model_validate(item) for item in raw_references
                )
        # 4. 交给构建器按 token 预算装配消息列表，并得到选取结果。
        messages, selection = self.builder.build(
            context=context,
            runtime_messages=request.messages,
            system_prompt=system_prompt,
            tools=tools,
            memory_references=memory_references,
        )
        # 5. 上报召回观测 span（最近原文、摘要与古老历史的入选数量）。
        trace_conversation_recall(
            conversation_id=context.conversation_id,
            recent_count=len(selection.recent_message_ids),
            summary_count=len(selection.summary_ids),
            historical_count=len(selection.historical_message_ids),
        )
        # 6. 读取会话日志，定位最近原文消息对应的日志序号范围。
        journal_messages = {
            item.message_id: item for item in self.repository.list_messages(context.conversation_id)
        }
        recent_sequences = [
            journal_messages[item_id].sequence
            for item_id in selection.recent_message_ids
            if item_id in journal_messages
        ]
        # 7. 解析工具目录，把暴露工具整理为 ``tool_id@version`` 治理标识。
        exposed_tools: list[str] = []
        for tool in tools:
            tool_id = str(getattr(tool, "name", "unknown"))
            try:
                managed = self.tool_catalog.resolve(tool_id)
                exposed_tools.append(f"{managed.governance.tool_id}@{managed.governance.version}")
            except LookupError:
                exposed_tools.append(tool_id)
        # 8. 组装 ModelContextManifest（含预算、省略明细与上下文哈希）并落库。
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
        # 9. 上报 Manifest 持久化观测 span。
        trace_manifest_persist(
            model_call_id=manifest.model_call_id,
            context_hash=manifest.context_hash,
        )
        # 10. 调试开启时输出脱敏后的完整最终上下文（development 调试用）。
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
        # 11. 返回以最终消息列表覆盖后的请求与本次 Manifest。
        return request.override(messages=messages), manifest

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """在同步模型调用外层完成上下文组装，并把 Manifest 信息写入追踪元数据。"""
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
        """异步版本：上下文组装在线程池中执行，其余行为与同步版本一致。

        Args:
            request: 模型调用请求。
            handler: 后续调用链，返回模型响应。

        Returns:
            ModelResponse: 模型响应。

        """
        prepared, manifest = await asyncio.to_thread(self._prepare, request)
        metadata = (
            {"model_call_id": manifest.model_call_id, "context_hash": manifest.context_hash}
            if manifest is not None
            else {}
        )
        with tracing_context(metadata=metadata):
            return await handler(prepared)
