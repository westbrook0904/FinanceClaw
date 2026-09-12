"""原生 state 与完整模型请求之间的纯投影及安全压缩范围。"""

import json
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from financeclaw.agent_server.context.state import WorkingContext
from financeclaw.kernel.turns import current_turn_start, is_user_message


def working_message(value: dict | None) -> HumanMessage | None:
    """从唯一 checkpoint 摘要临时渲染合成消息，绝不成为真实用户锚点。"""
    if not value:
        return None
    summary = WorkingContext.model_validate(value)
    return HumanMessage(
        id=f"working-context-{summary.summary_version}",
        content=(
            "Historical working context; not new user instructions or authorization.\n"
            + summary.model_dump_json()
        ),
        additional_kwargs={
            "lc_source": "summarization",
            "summary_source": {
                "version": summary.summary_version,
                "through_message_id": summary.source_boundary,
                "privacy_epoch": summary.privacy_epoch,
            },
        },
    )


def history_messages(turn) -> list[BaseMessage]:
    """重建业务会话事实，失败轮次只含原问题、已确认补充和明确的未完成状态。"""
    result = []
    for item in (turn.user, turn.assistant):
        if item is not None:
            result.append(
                (HumanMessage if item.role.value == "user" else AIMessage)(
                    content=item.content,
                    id=item.message_id,
                    additional_kwargs={
                        "financeclaw_source": {
                            "conversation_id": item.conversation_id,
                            "turn_id": item.turn_id,
                            "sequence": item.sequence,
                            "source": "journal_bootstrap",
                        }
                    },
                )
            )
    if turn.status != "completed" or turn.clarifications:
        status = {
            "completed": "本轮已完成，以上为最终回复。",
            "failed": "本轮处理失败，未获得有效的最终答案。用户的原始问题尚未完成。",
            "cancelled": "本轮已被用户停止，未获得最终答案，不能视为任务已完成。",
        }[turn.status]
        result.append(
            AIMessage(
                id=f"turn-outcome-{turn.user.turn_id}",
                content="历史任务记录（不构成新的授权）：\n"
                + json.dumps(
                    {
                        "status": turn.status,
                        "outcome": status,
                        "confirmed_clarifications": turn.clarifications,
                    },
                    ensure_ascii=False,
                ),
                additional_kwargs={
                    "financeclaw_source": {
                        "conversation_id": turn.user.conversation_id,
                        "turn_id": turn.user.turn_id,
                        "source": "turn_outcome",
                    }
                },
            )
        )
    return result


def projected_messages(state, *, system_prompt="", memory_projection=True) -> list[BaseMessage]:
    """准备与最终模型投影一致的系统、记忆、工作摘要和原生消息。"""
    system = SystemMessage(content=system_prompt) if system_prompt else None
    if memory_projection and "memory_recall" in state:
        system = memory_system_message(system, state["memory_recall"])
    result = [system] if system is not None else []
    working = working_message(state.get("working_context"))
    if working is not None:
        result.append(working)
    result.extend(state.get("messages", []))
    return result


def memory_system_message(existing, snapshot) -> SystemMessage:
    """预算准备与最终发送使用同一记忆正文和来源元数据布局。"""
    content = existing.content if existing is not None else ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return SystemMessage(
        content=content + snapshot.get("projection", ""),
        additional_kwargs={
            **(existing.additional_kwargs if existing is not None else {}),
            "financeclaw_memory_refs": snapshot.get("refs", []),
            "financeclaw_memory_omissions": snapshot.get("omissions", []),
            "memory_owner_revision": snapshot.get("owner_revision"),
            "memory_privacy_epoch": snapshot.get("privacy_epoch"),
        },
    )


def completed_tool_batches(messages: Sequence[BaseMessage]) -> list[tuple[int, ...]] | None:
    """返回完整工具批次；任何未配对调用或孤立结果都禁止此次压缩。"""
    batches = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, ToolMessage):
            return None
        if not isinstance(message, AIMessage) or not (
            message.tool_calls or message.invalid_tool_calls
        ):
            index += 1
            continue
        all_calls = [*message.tool_calls, *message.invalid_tool_calls]
        calls = {call["id"] for call in all_calls}
        if not all(calls) or len(calls) != len(all_calls):
            return None
        indices = [index]
        index += 1
        results = set()
        while index < len(messages) and isinstance(messages[index], ToolMessage):
            result = messages[index]
            if result.tool_call_id not in calls or result.tool_call_id in results:
                return None
            results.add(result.tool_call_id)
            indices.append(index)
            index += 1
        if calls != results:
            return None
        batches.append(tuple(indices))
    return batches


def compactable_indices(
    messages, anchor, *, counter, recent_tokens, recent_turns=0
) -> tuple[int, ...]:
    """保留真实当前输入与最近 token 区域，选择完整的较早执行片段。"""
    batches = completed_tool_batches(messages)
    if batches is None:
        return ()
    current = current_turn_start(messages, anchor)
    history_starts = [index for index in range(current) if is_user_message(messages[index])]
    history_cutoff = (
        history_starts[max(0, len(history_starts) - recent_turns)]
        if history_starts and recent_turns
        else current
    )
    selected = set(range(history_cutoff))
    budget = 0
    tail = len(messages)
    for index in range(len(messages) - 1, current, -1):
        cost = counter.message(messages[index])
        if budget + cost > recent_tokens:
            break
        budget += cost
        tail = index
    # Always retain the most recent batch; a large single result is handled by archive projection.
    if batches and batches[-1][0] >= current:
        tail = min(tail, batches[-1][0])
    batch_members = {index for batch in batches for index in batch}
    for batch in batches:
        if current < batch[0] and batch[-1] < tail:
            if not any(
                messages[index].additional_kwargs.get("preserve_structure")
                or (
                    isinstance(messages[index], ToolMessage)
                    and (messages[index].name or "").startswith("request_user__")
                )
                for index in batch
            ):
                selected.update(batch)
    for index in range(current + 1, tail):
        if index not in batch_members and isinstance(messages[index], AIMessage):
            selected.add(index)
    # True users and accepted clarification receipts in the current Turn survive byte-for-byte.
    selected.difference_update(
        index for index in range(current, len(messages)) if is_user_message(messages[index])
    )
    return tuple(sorted(selected))


def canonical_content(value) -> str:
    """序列化模型输入指纹，避免列表消息与文本消息采用不同字节规则。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
