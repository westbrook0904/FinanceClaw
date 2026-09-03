"""生成分层会话摘要并维护摘要替代关系。"""

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from .models import ConversationMessage, ConversationSummary
from .repository import SqlAlchemyConversationRepository

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9._-]{1,31}|[\u4e00-\u9fff]{2,}")
_TICKER = re.compile(r"\b[A-Z]{1,6}(?:[.-][A-Z]{1,3})?\b")


class DeterministicSummarizer:
    """定义DeterministicSummarizer。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        model_profile_version: 本次模型调用固定使用的模型配置版本。
        template_version: 生成该内容时使用的模板版本。
    """

    model_profile_version = "deterministic-summary/1.0.0"
    template_version = "conversation-summary/1.0.0"

    def summarize_messages(self, messages: Sequence[ConversationMessage]) -> str:
        """把连续原始消息压缩为结构化摘要，同时保留实体、决策与待办。"""
        lines = [f"{item.role.value}: {item.content.strip()}" for item in messages]
        return _bounded("\n".join(lines), 2_000)

    def summarize_summaries(self, summaries: Sequence[ConversationSummary]) -> str:
        """把相邻低层摘要合并为更高层摘要，并保留来源引用。"""
        lines = [
            f"historical segment {item.start_sequence}-{item.end_sequence}: {item.summary_content}"
            for item in summaries
        ]
        return _bounded("\n".join(lines), 3_000)


class SummaryService:
    """把达到阈值的消息或低层摘要压缩为可复现的分层摘要。

    适用场景：
        用于应用用例需要跨仓储、外部端口或领域策略协调一致结果的场景。

    属性：
        repository: 负责领域状态读写和事务一致性的仓储。
        summarizer: 把消息或低层摘要压缩为结构化摘要内容的实现。
        segment_messages: 生成一个最低层摘要所覆盖的消息数量。
        hierarchy_segments: 合并为一个高层摘要所需的相邻低层摘要数量。
    """

    def __init__(
        self,
        repository: SqlAlchemyConversationRepository,
        *,
        summarizer: DeterministicSummarizer | None = None,
        segment_messages: int = 12,
        hierarchy_segments: int = 8,
    ) -> None:
        """注入并保存摘要Service所需的协作对象，同时校验构造期不变量。"""
        if segment_messages < 2 or hierarchy_segments < 2:
            raise ValueError("summary thresholds must be at least two")
        self.repository = repository
        self.summarizer = summarizer or DeterministicSummarizer()
        self.segment_messages = segment_messages
        self.hierarchy_segments = hierarchy_segments

    def build_missing_segments(self, conversation_id: str) -> tuple[ConversationSummary, ...]:
        """根据已注入依赖组装摘要Service。"""
        messages = self.repository.list_messages(conversation_id)
        closed_count = (
            len(messages)
            if messages and messages[-1].role.value == "assistant"
            else len(messages) - 1
        )
        closed = messages[:closed_count]
        active = self.repository.list_summaries(conversation_id)
        covered_ranges = {
            (item.start_sequence, item.end_sequence) for item in active if item.level == 0
        }
        created: list[ConversationSummary] = []
        for offset in range(0, len(closed), self.segment_messages):
            chunk = closed[offset : offset + self.segment_messages]
            if len(chunk) < self.segment_messages:
                break
            source_range = (chunk[0].sequence, chunk[-1].sequence)
            if source_range in covered_ranges:
                continue
            created.append(self._message_summary(conversation_id, chunk))
        return tuple(created)

    def build_hierarchy(
        self, conversation_id: str, *, level: int = 1
    ) -> ConversationSummary | None:
        """根据已注入依赖组装摘要Service。"""
        if level < 1:
            raise ValueError("hierarchical summary level must be positive")
        lower = [
            item
            for item in self.repository.list_summaries(conversation_id)
            if item.level == level - 1
        ]
        if len(lower) < self.hierarchy_segments:
            return None
        higher_ranges = {
            (item.start_sequence, item.end_sequence)
            for item in self.repository.list_summaries(conversation_id)
            if item.level == level
        }
        for offset in range(0, len(lower), self.hierarchy_segments):
            chunk = lower[offset : offset + self.hierarchy_segments]
            if len(chunk) < self.hierarchy_segments:
                break
            source_range = (chunk[0].start_sequence, chunk[-1].end_sequence)
            if source_range in higher_ranges:
                continue
            content = self.summarizer.summarize_summaries(chunk)
            summary = self._new_summary(
                conversation_id=conversation_id,
                level=level,
                start_sequence=chunk[0].start_sequence,
                end_sequence=chunk[-1].end_sequence,
                source_summary_ids=tuple(item.summary_id for item in chunk),
                content=content,
            )
            return self.repository.save_summary(summary)
        return None

    def rebuild(self, summary_id: str) -> ConversationSummary:
        """清理旧摘要的当前态后，从消息 Journal 重新生成完整摘要层级。"""
        old = self.repository.get_summary(summary_id)
        conversation_id = old.conversation_id
        all_summaries = list(self.repository.list_summaries(conversation_id, active_only=False))
        old = next(item for item in all_summaries if item.summary_id == summary_id)
        if old.level == 0:
            sources = {
                message.message_id: message
                for message in self.repository.list_messages(conversation_id)
            }
            messages = [sources[item_id] for item_id in old.source_message_ids]
            content = self.summarizer.summarize_messages(messages)
            replacement = self._new_summary(
                conversation_id=conversation_id,
                level=0,
                start_sequence=old.start_sequence,
                end_sequence=old.end_sequence,
                source_message_ids=old.source_message_ids,
                content=content,
            )
        else:
            by_id = {item.summary_id: item for item in all_summaries}
            sources = [by_id[item_id] for item_id in old.source_summary_ids]
            content = self.summarizer.summarize_summaries(sources)
            replacement = self._new_summary(
                conversation_id=conversation_id,
                level=old.level,
                start_sequence=old.start_sequence,
                end_sequence=old.end_sequence,
                source_summary_ids=old.source_summary_ids,
                content=content,
            )
        return self.repository.save_summary(replacement, supersede_ids=(old.summary_id,))

    def _message_summary(
        self, conversation_id: str, messages: Sequence[ConversationMessage]
    ) -> ConversationSummary:
        """把一段连续消息交给摘要器，并构造带序号范围和来源证据的摘要。"""
        content = self.summarizer.summarize_messages(messages)
        summary = self._new_summary(
            conversation_id=conversation_id,
            level=0,
            start_sequence=messages[0].sequence,
            end_sequence=messages[-1].sequence,
            source_message_ids=tuple(item.message_id for item in messages),
            content=content,
        )
        return self.repository.save_summary(summary)

    def _new_summary(
        self,
        *,
        conversation_id: str,
        level: int,
        start_sequence: int,
        end_sequence: int,
        content: str,
        source_message_ids: tuple[str, ...] = (),
        source_summary_ids: tuple[str, ...] = (),
    ) -> ConversationSummary:
        """根据摘要内容、来源和版本生成确定性标识及完整领域记录。"""
        terms = tuple(dict.fromkeys(term.lower() for term in _WORD.findall(content)))[:24]
        entities = tuple(dict.fromkeys(_TICKER.findall(content)))[:16]
        return ConversationSummary(
            summary_id=f"summary-{uuid4().hex}",
            conversation_id=conversation_id,
            level=level,
            start_sequence=start_sequence,
            end_sequence=end_sequence,
            source_message_ids=source_message_ids,
            source_summary_ids=source_summary_ids,
            summary_content=content,
            topics=terms,
            entities=entities,
            model_profile_version=self.summarizer.model_profile_version,
            template_version=self.summarizer.template_version,
            content_hash=sha256(content.encode()).hexdigest(),
            created_at=datetime.now(UTC),
        )


def _bounded(value: str, limit: int) -> str:
    """按字段上限截断并去重提取值，避免摘要元数据无限增长。"""
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"
