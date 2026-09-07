"""用户交互模块：已发布的问题契约、原生中断实例与持久化回答。

models 区分资料、选项和审批回答；repository 保证决定、审批镜像、恢复操作及审计
同事务保存。发布校验与远程恢复由 application.interaction_service 协调，HTTP 和
飞书只适配展示及输入格式，不能各自实现一套审批规则。
"""

from .models import InteractionPoint, InteractionResponse
from .repository import InteractionConflict, InteractionNotFound, InteractionRepository

__all__ = [
    "InteractionPoint",
    "InteractionResponse",
    "InteractionConflict",
    "InteractionNotFound",
    "InteractionRepository",
]
