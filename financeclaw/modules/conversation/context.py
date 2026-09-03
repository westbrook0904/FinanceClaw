"""模型调用上下文装配器：按 token 预算选取最近原文、摘要与相关古老历史。

基于 Conversation Journal 组装进入模型调用的消息列表与 ContextSelection，
记录省略明细与上下文哈希，供 Manifest 持久化与 development 环境调试复现。
"""

import json
import os
import re
import tempfile
from collections.abc import Sequence
from hashlib import sha1, sha256
from pathlib import Path
from typing import Any

import tiktoken
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from financeclaw.kernel import ExecutionContext

from .models import (
    ContextOmission,
    ContextSelection,
    ConversationMessage,
    ConversationSummary,
    ManifestMemoryReference,
)
from .repository import ConversationRepository

# 相关性检索的分词模式：匹配 2 位以上的英文/数字/点划线词，或连续中文片段。
_TERM = re.compile(r"[A-Za-z0-9._-]{2,}|[\u4e00-\u9fff]{2,}")


class ContextBudget(BaseModel):
    """进入模型调用的输入 token 预算配置（不可变）。

    使用场景：bootstrap 阶段依据模型输入上限与各类预留构建，由
    ConversationContextBuilder 用其约束每次装配可用的历史上下文规模。

    Attributes:
        model_config: Pydantic 模型配置；extra="forbid" 拒绝未知字段，
            frozen=True 保证配置不可变。
        model_input_limit: 模型最大输入 token 上限，至少 1024。
        reserved_output_tokens: 为模型输出预留的 token 数，至少 64。
        system_policy_reserve: 为系统提示（策略部分）预留的 token 数，至少 0。
        tool_schema_reserve: 为工具 schema 预留的 token 数，至少 0。
        safety_margin: 额外安全余量，吸收计数误差，至少 0。
        max_recent_messages: 最近原文窗口最多保留的消息条数，默认 12（1-1000）。
        max_relevant_summaries: 按相关性最多入选的摘要条数，默认 4（0-64）。
        max_relevant_messages: 按相关性最多入选的古老历史消息条数，默认 4（0-64）。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_input_limit: int = Field(ge=1_024)
    reserved_output_tokens: int = Field(ge=64)
    system_policy_reserve: int = Field(ge=0)
    tool_schema_reserve: int = Field(ge=0)
    safety_margin: int = Field(ge=0)
    max_recent_messages: int = Field(default=12, ge=1, le=1_000)
    max_relevant_summaries: int = Field(default=4, ge=0, le=64)
    max_relevant_messages: int = Field(default=4, ge=0, le=64)

    @property
    def available_input_tokens(self) -> int:
        """计算扣除全部预留后，可用于历史上下文的输入 token 数。

        使用场景：装配前确定预算基数；构造时若该值低于 256 会拒绝创建。

        Returns:
            int: 输入上限减去输出预留、系统预留、工具预留与安全余量后的剩余值。

        """
        return (
            self.model_input_limit
            - self.reserved_output_tokens
            - self.system_policy_reserve
            - self.tool_schema_reserve
            - self.safety_margin
        )

    @model_validator(mode="after")
    def validate_available_budget(self) -> "ContextBudget":
        """校验扣除预留后的可用输入预算不低于 256 token。

        使用场景：构造 ContextBudget 时自动执行，避免配置失衡导致上下文无法装配。

        Returns:
            ContextBudget: 校验通过后的实例。

        Raises:
            ValueError: 可用输入 token 少于 256 时抛出。

        """
        if self.available_input_tokens < 256:
            raise ValueError("context reserves leave fewer than 256 input tokens")
        return self


class TokenCounter:
    """token 计数与截断工具：优先用 tiktoken 精确计数，退化时按 UTF-8 字节估算。

    使用场景：ConversationContextBuilder 用其统计系统提示与消息的 token 占用，
    并在预算不足时按 token 边界截断当前输入或工具结果正文。
    """

    def __init__(self) -> None:
        """初始化计数器：tiktoken 可用时加载 cl100k_base 编码，否则留空退化。"""
        self._encoding = None
        if _tiktoken_cache_available():
            try:
                self._encoding = tiktoken.get_encoding("cl100k_base")
            except Exception:
                self._encoding = None

    def text(self, value: str) -> int:
        """统计一段文本的 token 数。

        Args:
            value: 待统计文本。

        Returns:
            int: tiktoken 可用时返回精确 token 数，否则返回 UTF-8 字节数估算值。

        """
        if self._encoding is not None:
            return len(self._encoding.encode(value))
        return _estimated_tokens(value)

    def message(self, message: BaseMessage) -> int:
        """统计一条 LangChain 消息序列化后的 token 数。

        Args:
            message: 待统计的消息对象。

        Returns:
            int: 消息 JSON 序列化结果的 token 数，另加 4 个 token 的消息边界开销。

        """
        payload = json.dumps(message.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        return 4 + self.text(payload)

    def truncate(self, value: str, max_tokens: int) -> str:
        """将文本截断到不超过指定 token 数，保留最靠前的内容。

        使用场景：预算不足时截断当前用户输入或工具结果正文。

        Args:
            value: 原始文本。
            max_tokens: 允许的最大 token 数；不大于 0 时返回空字符串。

        Returns:
            str: 未超限时返回原文；超限时按 token 边界（或估算二分）截断的前缀。

        """
        if max_tokens <= 0:
            return ""
        if self._encoding is not None:
            tokens = self._encoding.encode(value)
            if len(tokens) <= max_tokens:
                return value
            return self._encoding.decode(tokens[:max_tokens])
        if self.text(value) <= max_tokens:
            return value
        low, high = 0, len(value)
        while low < high:
            midpoint = (low + high + 1) // 2
            if self.text(value[:midpoint]) <= max_tokens:
                low = midpoint
            else:
                high = midpoint - 1
        return value[:low]


def _estimated_tokens(value: str) -> int:
    """退化场景下的 token 估算：直接以 UTF-8 编码字节数作为 token 数。

    Args:
        value: 待估算文本。

    Returns:
        int: 估算 token 数（UTF-8 字节数）。

    """
    return len(value.encode("utf-8"))


def _tiktoken_cache_available() -> bool:
    """检查本地是否已有 cl100k_base 编码缓存，避免初始化时联网下载。

    使用场景：TokenCounter 初始化前探测；离线环境据此退化为字节估算。

    Returns:
        bool: 缓存目录未显式置空且编码缓存文件存在时返回 True。

    """
    cache_root = os.getenv("TIKTOKEN_CACHE_DIR") or os.getenv("DATA_GYM_CACHE_DIR")
    if cache_root == "":
        return False
    root = Path(cache_root) if cache_root else Path(tempfile.gettempdir()) / "data-gym-cache"
    source = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
    return (root / sha1(source.encode()).hexdigest()).is_file()


class ConversationContextBuilder:
    """按 token 预算从 Conversation Journal 装配模型调用上下文的构建器。

    使用场景：上下文中间件在每次模型调用前调用 build，得到由最近原文、摘要与
    相关古老历史组成的消息列表，以及用于 Manifest 持久化的 ContextSelection。

    Attributes:
        repository: 会话日志仓库，用于读取原文消息与摘要。
        budget: 输入 token 预算配置，见 ContextBudget。
        counter: token 计数器；缺省时新建 TokenCounter，可注入以便测试。

    """

    def __init__(
        self,
        repository: ConversationRepository,
        budget: ContextBudget,
        *,
        counter: TokenCounter | None = None,
    ) -> None:
        """保存仓库、预算配置与计数器；未注入计数器时新建默认实例。"""
        self.repository = repository
        self.budget = budget
        self.counter = counter or TokenCounter()

    def build(
        self,
        *,
        context: ExecutionContext,
        runtime_messages: Sequence[AnyMessage],
        system_prompt: str,
        tools: Sequence[Any],
        memory_references: Sequence[ManifestMemoryReference] = (),
    ) -> tuple[list[AnyMessage], ContextSelection]:
        """装配一次模型调用的完整上下文，返回最终消息列表与选取结果。

        使用场景：每次模型调用前调用；完成预算计算、当前输入适配、最近窗口
        裁剪、摘要与古老历史的相关性选择，并记录全部省略明细。

        Args:
            context: 执行上下文，必须携带 conversation_id。
            runtime_messages: 本次运行的实时消息序列（含最新用户输入与工具往返）。
            system_prompt: 系统提示文本。
            tools: 本次暴露给模型的工具序列。
            memory_references: 注入的记忆引用，默认为空。

        Returns:
            tuple[list[AnyMessage], ContextSelection]: 装配后的消息列表与选取结果。

        Raises:
            ValueError: 缺少 conversation_id，或系统提示、工具 schema、当前输入
                超出全部可用预算时抛出。

        """
        # 1. 校验会话标识，并从日志读取全部原文消息与活跃摘要。
        if context.conversation_id is None:
            raise ValueError("conversation_id is required for journal context selection")
        journal = self.repository.list_messages(context.conversation_id)
        summaries = self.repository.list_summaries(context.conversation_id)
        # 2. 取最后一条用户消息起的运行时后缀，并以最新用户输入作为相关性查询词。
        suffix = _current_runtime_suffix(runtime_messages)
        query = _latest_user_text(suffix) or (journal[-1].content if journal else "")
        serialized_tools = [
            {
                "name": getattr(tool, "name", str(tool)),
                "schema": getattr(tool, "args", None),
            }
            for tool in tools
        ]
        # 3. 统计系统提示与工具 schema 的 token 占用，扣除后得到剩余可用预算。
        system_tokens = self.counter.text(system_prompt)
        tool_tokens = self.counter.text(
            json.dumps(serialized_tools, ensure_ascii=False, sort_keys=True, default=str)
        )
        reserve_overflow = max(0, system_tokens - self.budget.system_policy_reserve) + max(
            0, tool_tokens - self.budget.tool_schema_reserve
        )
        remaining = self.budget.available_input_tokens - reserve_overflow
        if remaining < 1:
            raise ValueError("mandatory system and tool context exceeds configured input budget")
        omissions: list[ContextOmission] = []

        # 4. 先保障当前运行时后缀进入上下文，必要时截断其中的用户输入或工具结果。
        suffix, suffix_tokens, suffix_omissions = self._fit_runtime_suffix(suffix, remaining)
        remaining -= suffix_tokens
        omissions.extend(suffix_omissions)

        # 5. 剔除与当前输入重复的日志消息，划定最近窗口与其余的古老历史。
        duplicate_current = (
            journal[-1].message_id if journal and _matches_current(journal[-1], suffix) else None
        )
        eligible_journal = [item for item in journal if item.message_id != duplicate_current]
        recent_candidates = eligible_journal[-self.budget.max_recent_messages :]
        older = (
            eligible_journal[: -len(recent_candidates)] if recent_candidates else eligible_journal
        )

        # 6. 从最近窗口尾部按预算倒序保留原文，超预算的消息记录省略。
        recent_selected: list[ConversationMessage] = []
        for item in reversed(recent_candidates):
            tokens = self.counter.message(_to_message(item))
            if tokens <= remaining:
                recent_selected.append(item)
                remaining -= tokens
            else:
                omissions.append(
                    ContextOmission(
                        reason="token_budget",
                        item_type="message",
                        item_id=item.message_id,
                        token_count=tokens,
                    )
                )
        recent_selected.reverse()
        # 7. 未入选的最近窗口消息按"recent_window"原因补充省略记录。
        selected_recent_ids = {item.message_id for item in recent_selected}
        for item in recent_candidates:
            if item.message_id not in selected_recent_ids and not any(
                omission.item_id == item.message_id for omission in omissions
            ):
                omissions.append(
                    ContextOmission(
                        reason="recent_window",
                        item_type="message",
                        item_id=item.message_id,
                        token_count=self.counter.message(_to_message(item)),
                    )
                )

        # 8. 按查询相关性为摘要排序，并在剩余预算内依次入选。
        relevant_summaries = _rank_summaries(query, summaries)[: self.budget.max_relevant_summaries]
        selected_summaries: list[ConversationSummary] = []
        for item in relevant_summaries:
            message = _summary_message(item)
            tokens = self.counter.message(message)
            if tokens <= remaining:
                selected_summaries.append(item)
                remaining -= tokens
            else:
                omissions.append(
                    ContextOmission(
                        reason="token_budget",
                        item_type="summary",
                        item_id=item.summary_id,
                        token_count=tokens,
                    )
                )

        # 9. 按查询相关性为窗口外的古老历史排序，并在剩余预算内依次入选。
        relevant_old = _rank_messages(query, older)[: self.budget.max_relevant_messages]
        selected_old: list[ConversationMessage] = []
        for item in relevant_old:
            message = _to_message(item)
            tokens = self.counter.message(message)
            if tokens <= remaining:
                selected_old.append(item)
                remaining -= tokens
            else:
                omissions.append(
                    ContextOmission(
                        reason="token_budget",
                        item_type="message",
                        item_id=item.message_id,
                        token_count=tokens,
                    )
                )

        # 10. 古老历史按序号归位，拼接摘要、古老历史、最近原文与当前输入。
        selected_old.sort(key=lambda item: item.sequence)
        messages: list[AnyMessage] = [
            *(_summary_message(item) for item in selected_summaries),
            *(_to_message(item) for item in selected_old),
            *(_to_message(item) for item in recent_selected),
            *suffix,
        ]
        # 11. 计算输入 token 总数与上下文哈希，收集被外置的工件引用并记录省略。
        input_tokens = (
            system_tokens + tool_tokens + sum(self.counter.message(item) for item in messages)
        )
        canonical = json.dumps(
            {
                "system": system_prompt,
                "messages": [item.model_dump(mode="json") for item in messages],
                "tools": serialized_tools,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        context_hash = sha256(canonical.encode()).hexdigest()
        tool_refs = tuple(
            str(item.additional_kwargs["artifact_ref"]["artifact_id"])
            for item in suffix
            if isinstance(item, ToolMessage)
            and isinstance(item.additional_kwargs.get("artifact_ref"), dict)
        )
        for item in suffix:
            reference = item.additional_kwargs.get("artifact_ref")
            if isinstance(item, ToolMessage) and isinstance(reference, dict):
                omissions.append(
                    ContextOmission(
                        reason="artifact_offloaded",
                        item_type="tool_result",
                        item_id=str(reference.get("artifact_id", item.id or "artifact")),
                        token_count=max(0, int(reference.get("size_bytes", 0)) // 4),
                    )
                )
        # 12. 汇总为 ContextSelection（含调试负载）返回。
        selection = ContextSelection(
            recent_message_ids=tuple(item.message_id for item in recent_selected),
            summary_ids=tuple(item.summary_id for item in selected_summaries),
            memory_refs=tuple(memory_references),
            historical_message_ids=tuple(item.message_id for item in selected_old),
            tool_result_refs=tool_refs,
            input_token_count=input_tokens,
            available_input_tokens=self.budget.available_input_tokens,
            omissions=tuple(omissions),
            context_hash=context_hash,
            debug_payload={
                "system_prompt": system_prompt,
                "messages": [item.model_dump(mode="json") for item in messages],
                "tools": serialized_tools,
                "token_budget": self.budget.model_dump(mode="json"),
            },
        )
        return messages, selection

    def _fit_runtime_suffix(
        self, suffix: list[AnyMessage], remaining: int
    ) -> tuple[list[AnyMessage], int, list[ContextOmission]]:
        """在剩余预算内适配当前运行时消息，必要时截断用户输入或工具结果正文。

        使用场景：build 内部调用；仅对 HumanMessage 与 ToolMessage 的字符串正文
        做 token 边界截断，其余类型的消息不可裁剪。

        Args:
            suffix: 当前运行时消息列表。
            remaining: 可用的剩余 token 数。

        Returns:
            tuple[list[AnyMessage], int, list[ContextOmission]]:
                适配后的消息列表、其 token 总数与截断产生的省略记录。

        Raises:
            ValueError: 截断后仍超出预算（存在不可裁剪的必要消息）时抛出。

        """
        # 1. 统计整体占用，未超预算时原样返回。
        total = sum(self.counter.message(item) for item in suffix)
        if total <= remaining:
            return suffix, total, []
        fitted = list(suffix)
        omissions: list[ContextOmission] = []
        # 2. 逐条对用户输入与工具结果正文截断，目标值为扣除其余消息后可用的 token 数。
        for index, item in enumerate(fitted):
            tokens = self.counter.message(item)
            if total <= remaining:
                break
            if isinstance(item, (HumanMessage, ToolMessage)) and isinstance(item.content, str):
                content_tokens = self.counter.text(item.content)
                message_overhead = tokens - content_tokens
                target = max(0, remaining - (total - tokens) - message_overhead)
                truncated = self.counter.truncate(item.content, target)
                fitted[index] = item.model_copy(update={"content": truncated})
                new_tokens = self.counter.message(fitted[index])
                total -= tokens - new_tokens
                omissions.append(
                    ContextOmission(
                        reason=(
                            "current_input_truncated"
                            if isinstance(item, HumanMessage)
                            else "token_budget"
                        ),
                        item_type=(
                            "current_input" if isinstance(item, HumanMessage) else "tool_result"
                        ),
                        item_id=str(getattr(item, "id", None) or f"runtime-{index}"),
                        token_count=tokens - new_tokens,
                    )
                )
        # 3. 全部截断后仍超预算，说明存在不可裁剪的必要上下文，直接失败。
        if total > remaining:
            raise ValueError("mandatory current model context exceeds configured input budget")
        return fitted, total, omissions


def _current_runtime_suffix(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """提取自最后一条用户消息起的运行时消息后缀。

    使用场景：build 中确定"当前输入"边界；后缀内的消息必须完整进入本次调用。

    Args:
        messages: 完整的运行时消息序列。

    Returns:
        list[AnyMessage]: 最后一条 HumanMessage 及其后的全部消息；
            无用户消息时返回全部消息。

    """
    last_human = 0
    for index, item in enumerate(messages):
        if isinstance(item, HumanMessage):
            last_human = index
    return list(messages[last_human:])


def _latest_user_text(messages: Sequence[AnyMessage]) -> str:
    """返回消息序列中最后一条用户输入文本，作为相关性检索的查询词。

    Args:
        messages: 运行时消息序列。

    Returns:
        str: 最后一条字符串内容 HumanMessage 的文本；不存在时返回空字符串。

    """
    for item in reversed(messages):
        if isinstance(item, HumanMessage) and isinstance(item.content, str):
            return item.content
    return ""


def _matches_current(message: ConversationMessage, suffix: Sequence[AnyMessage]) -> bool:
    """判断日志最后一条消息是否与当前用户输入重复（同角色且同内容）。

    使用场景：build 中避免当前输入在日志与实时消息里重复出现。

    Args:
        message: 日志中的候选消息。
        suffix: 当前运行时消息后缀。

    Returns:
        bool: 后缀首条为字符串 HumanMessage 且与该日志消息内容一致时返回 True。

    """
    return bool(
        suffix
        and isinstance(suffix[0], HumanMessage)
        and isinstance(suffix[0].content, str)
        and message.role.value == "user"
        and message.content == suffix[0].content
    )


def _to_message(item: ConversationMessage) -> AnyMessage:
    """将日志中的原文消息转换为 LangChain 消息对象。

    Args:
        item: 日志中的原文消息记录。

    Returns:
        AnyMessage: user 角色转为 HumanMessage，其余转为 AIMessage，
            并保留原消息 ID 以便追踪。

    """
    if item.role.value == "user":
        return HumanMessage(content=item.content, id=item.message_id)
    return AIMessage(content=item.content, id=item.message_id)


def _summary_message(item: ConversationSummary) -> SystemMessage:
    """将摘要记录包装为注入上下文的 SystemMessage。

    使用场景：build 中把入选摘要包装为系统消息，标注覆盖的序号区间，
    并提示历史事实可能过时。

    Args:
        item: 摘要记录。

    Returns:
        SystemMessage: 带历史摘要前缀与摘要 ID 的系统消息。

    """
    return SystemMessage(
        content=(
            f"Historical conversation summary (sequences {item.start_sequence}-"
            f"{item.end_sequence}; historical facts may be stale): {item.summary_content}"
        ),
        id=item.summary_id,
    )


def _terms(value: str) -> set[str]:
    """对文本分词并统一小写，得到用于相关性匹配的词集合。

    Args:
        value: 待分词文本。

    Returns:
        set[str]: 命中 _TERM 模式的小写词集合。

    """
    return {term.lower() for term in _TERM.findall(value)}


def _score(query: str, value: str) -> tuple[int, int]:
    """计算查询与候选文本的相关性得分，供摘要与古老历史排序使用。

    Args:
        query: 当前用户输入文本。
        value: 候选文本。

    Returns:
        tuple[int, int]: （词交集数量, 候选文本长度）；
            排序时先按交集降序，再按长度升序。

    """
    overlap = _terms(query).intersection(_terms(value))
    return len(overlap), len(value)


def _rank_summaries(
    query: str, summaries: Sequence[ConversationSummary]
) -> list[ConversationSummary]:
    """按相关性降序返回与查询有词交集的摘要，过滤无交集项。

    Args:
        query: 当前用户输入文本。
        summaries: 候选摘要序列。

    Returns:
        list[ConversationSummary]: 排序后的相关摘要列表。

    """
    scored = [(item, _score(query, item.summary_content)) for item in summaries]
    return [
        item
        for item, score in sorted(scored, key=lambda pair: pair[1], reverse=True)
        if score[0] > 0
    ]


def _rank_messages(
    query: str, messages: Sequence[ConversationMessage]
) -> list[ConversationMessage]:
    """按相关性降序返回与查询有词交集的古老历史消息，过滤无交集项。

    Args:
        query: 当前用户输入文本。
        messages: 候选历史消息序列。

    Returns:
        list[ConversationMessage]: 排序后的相关消息列表。

    """
    scored = [(item, _score(query, item.content)) for item in messages]
    return [
        item
        for item, score in sorted(scored, key=lambda pair: pair[1], reverse=True)
        if score[0] > 0
    ]
