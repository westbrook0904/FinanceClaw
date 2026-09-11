"""执行端 Turn 定位与消息来源适配。"""

from collections.abc import Sequence
from typing import Any

from financeclaw.kernel.context import ExecutionContext
from financeclaw.kernel.turns import current_turn_start, is_user_message


def trusted_context(runtime: Any) -> ExecutionContext:
    """解析服务注入的身份，不从模型消息中推断归属。"""
    value = runtime.context
    return value if isinstance(value, ExecutionContext) else ExecutionContext.model_validate(value)


def user_anchor(context: ExecutionContext, repository: Any) -> str | None:
    """生产执行使用业务快照冻结的当前用户消息 ID。"""
    execution = getattr(repository, "execution", None)
    if context.turn_id and execution is not None:
        execution.verify_context(context)
        snapshot = execution.get(context.turn_id)["release_snapshot"]
        anchor = snapshot.get("user_message_id")
        if not anchor:
            raise ValueError("business root snapshot is missing its user message anchor")
        return anchor
    return None


def protected_start(messages: Sequence[Any], anchor: str | None, recent_turns: int) -> int:
    """保留当前完整 Turn 及指定数量的近期完整 Turn。"""
    current = current_turn_start(messages, anchor)
    starts = [index for index in range(current) if is_user_message(messages[index])]
    return starts[max(0, len(starts) - recent_turns)] if starts and recent_turns else current
