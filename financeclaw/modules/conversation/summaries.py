"""会话摘要服务：负责分段与分层摘要的生成、重建与落库策略。

DeterministicSummarizer 提供确定性摘要文本生成，SummaryService 基于
Conversation Journal 计算缺失分段、聚合层级并支持按需重建。
"""

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from .models import ConversationMessage, ConversationSummary
from .repository import SqlAlchemyConversationRepository

# 主题词提取模式：英文单词（2-32 位，允许点划线与数字）或连续中文片段。
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9._-]{1,31}|[\u4e00-\u9fff]{2,}")
# 实体（股票代码）提取模式：1-6 位大写字母，允许带一段 1-3 位后缀（如 BRK.B）。
_TICKER = re.compile(r"\b[A-Z]{1,6}(?:[.-][A-Z]{1,3})?\b")


class DeterministicSummarizer:
    """确定性摘要生成器：不依赖模型，按固定模板拼接摘要文本。

    使用场景：SummaryService 的默认摘要器；输出版本稳定（见类常量），
    便于审计与重建时的内容对账。

    Attributes:
        model_profile_version: 摘要器配置版本标识（"deterministic-summary/1.0.0"）。
        template_version: 摘要模板版本标识（"conversation-summary/1.0.0"）。

    """

    model_profile_version = "deterministic-summary/1.0.0"
    template_version = "conversation-summary/1.0.0"

    def summarize_messages(self, messages: Sequence[ConversationMessage]) -> str:
        """将一组原文消息拼接为分段摘要文本，长度上限 2000 字符。

        Args:
            messages: 参与摘要的原文消息序列。

        Returns:
            str: 形如 "role: content" 的逐行文本，超长时以省略号截断。

        """
        lines = [f"{item.role.value}: {item.content.strip()}" for item in messages]
        return _bounded("\n".join(lines), 2_000)

    def summarize_summaries(self, summaries: Sequence[ConversationSummary]) -> str:
        """将一组低层摘要聚合为更高层级的摘要文本，长度上限 3000 字符。

        Args:
            summaries: 低层摘要序列。

        Returns:
            str: 逐行标注历史区间的摘要拼接文本，超长时以省略号截断。

        """
        lines = [
            f"historical segment {item.start_sequence}-{item.end_sequence}: {item.summary_content}"
            for item in summaries
        ]
        return _bounded("\n".join(lines), 3_000)


class SummaryService:
    """摘要服务：计算缺失分段摘要、聚合分层摘要并支持按需重建。

    使用场景：turn 完成后由会话服务调用 build_missing_segments 与 build_hierarchy
    增量生成摘要；摘要策略需要修正时用 rebuild 基于源数据重新生成。
    构造时要求两个阈值均不小于 2，否则抛出 ValueError。

    Attributes:
        repository: 会话日志仓库，用于读取消息/摘要与保存新摘要。
        summarizer: 摘要文本生成器，默认为 DeterministicSummarizer。
        segment_messages: 单个分段摘要覆盖的最大消息条数，默认 12。
        hierarchy_segments: 聚合一个高层摘要所需的低层摘要条数，默认 8。

    """

    def __init__(
        self,
        repository: SqlAlchemyConversationRepository,
        *,
        summarizer: DeterministicSummarizer | None = None,
        segment_messages: int = 12,
        hierarchy_segments: int = 8,
    ) -> None:
        """校验阈值合法后保存仓库、摘要器与分段/分层阈值；未注入时用默认值。"""
        if segment_messages < 2 or hierarchy_segments < 2:
            raise ValueError("summary thresholds must be at least two")
        self.repository = repository
        self.summarizer = summarizer or DeterministicSummarizer()
        self.segment_messages = segment_messages
        self.hierarchy_segments = hierarchy_segments

    def build_missing_segments(self, conversation_id: str) -> tuple[ConversationSummary, ...]:
        """为尚未覆盖的完整分段生成分段摘要（level=0）并落库。

        使用场景：每次 turn 完成后调用；仅对以 assistant 回复收尾的"已封闭"
        前缀生成分段，半开的最后一段留待后续消息补齐。

        Args:
            conversation_id: 会话标识。

        Returns:
            tuple[ConversationSummary, ...]: 本次新建的分段摘要元组；无缺失时为空。

        """
        # 1. 读取全部消息，确定已封闭范围（最后一条不是 assistant 回复时回退一条）。
        messages = self.repository.list_messages(conversation_id)
        closed_count = (
            len(messages)
            if messages and messages[-1].role.value == "assistant"
            else len(messages) - 1
        )
        closed = messages[:closed_count]
        # 2. 收集活跃 level=0 摘要已覆盖的序号区间，避免重复生成。
        active = self.repository.list_summaries(conversation_id)
        covered_ranges = {
            (item.start_sequence, item.end_sequence) for item in active if item.level == 0
        }
        # 3. 按固定步长切块，跳过不足一个分段与已覆盖区间，生成并保存缺失摘要。
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
        """将低层摘要按固定数量聚合生成分层摘要（level>=1）并落库。

        使用场景：分段摘要累积到 hierarchy_segments 条后调用；每次调用至多
        生成一个高层摘要，低层不足或区间已覆盖时返回 None。

        Args:
            conversation_id: 会话标识。
            level: 目标摘要层级，默认 1，必须为正整数。

        Returns:
            ConversationSummary | None: 新生成的分层摘要；无可聚合内容时为 None。

        Raises:
            ValueError: level 小于 1 时抛出。

        """
        if level < 1:
            raise ValueError("hierarchical summary level must be positive")
        # 1. 读取 level-1 层摘要，数量不足一个聚合粒度时直接返回 None。
        lower = [
            item
            for item in self.repository.list_summaries(conversation_id)
            if item.level == level - 1
        ]
        if len(lower) < self.hierarchy_segments:
            return None
        # 2. 收集当前层级已覆盖的区间，避免重复聚合。
        higher_ranges = {
            (item.start_sequence, item.end_sequence)
            for item in self.repository.list_summaries(conversation_id)
            if item.level == level
        }
        # 3. 顺序切块聚合，命中已覆盖区间则跳过，生成首个缺失的高层摘要并保存。
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
        """基于源数据重建指定摘要，并将原摘要标记为被取代。

        使用场景：摘要策略升级或内容修复时调用；level=0 摘要从源消息重建，
        更高层摘要从其源摘要重建。

        Args:
            summary_id: 待重建的摘要标识。

        Returns:
            ConversationSummary: 新生成的摘要记录（原摘要置为 SUPERSEDED）。

        Raises:
            ConversationNotFound: 指定摘要不存在时抛出（由仓库抛出）。
            StopIteration: 摘要列表中找不到目标摘要时抛出（数据不一致的兜底路径）。
            KeyError: 源消息或源摘要被删除导致引用缺失时抛出。

        """
        # 1. 读取目标摘要（含被取代项）并重新定位，确保基于当前库内状态重建。
        old = self.repository.get_summary(summary_id)
        conversation_id = old.conversation_id
        all_summaries = list(self.repository.list_summaries(conversation_id, active_only=False))
        old = next(item for item in all_summaries if item.summary_id == summary_id)
        if old.level == 0:
            # 2. level=0：按源消息 ID 取回原文并重新生成分段摘要。
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
            # 3. level>=1：按源摘要 ID 取回低层摘要并重新聚合。
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
        # 4. 保存新摘要并将原摘要标记为被取代。
        return self.repository.save_summary(replacement, supersede_ids=(old.summary_id,))

    def _message_summary(
        self, conversation_id: str, messages: Sequence[ConversationMessage]
    ) -> ConversationSummary:
        """由一组原文消息构造并保存一条 level=0 分段摘要。

        Args:
            conversation_id: 会话标识。
            messages: 该分段覆盖的原文消息序列（按序号升序）。

        Returns:
            ConversationSummary: 已落库的分段摘要记录。

        """
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
        """构造新的摘要记录，并从摘要文本提取主题词与实体。

        使用场景：build_missing_segments、build_hierarchy 与 rebuild 共用的
        记录工厂；主题词取前 24 个去重词项，实体（股票代码）取前 16 个去重项。

        Args:
            conversation_id: 会话标识。
            level: 摘要层级。
            start_sequence: 覆盖起始序号。
            end_sequence: 覆盖结束序号。
            content: 摘要正文。
            source_message_ids: 源消息 ID 元组，level=0 时使用。
            source_summary_ids: 源摘要 ID 元组，level>=1 时使用。

        Returns:
            ConversationSummary: 尚未落库的摘要记录（内容哈希与时间戳已填充）。

        """
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
    """将文本压缩多余空白后限制到指定长度，超长时以省略号结尾。

    Args:
        value: 原始文本。
        limit: 最大字符数。

    Returns:
        str: 未超限时返回压缩空白后的文本；超限时截断并以"…"结尾。

    """
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"
