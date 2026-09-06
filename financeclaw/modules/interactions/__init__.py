"""用户交互的发布契约与持久化事实；不实现图调度。"""

from .models import InteractionPoint, InteractionResponse
from .repository import InteractionConflict, InteractionNotFound, InteractionRepository

__all__ = [
    "InteractionPoint",
    "InteractionResponse",
    "InteractionConflict",
    "InteractionNotFound",
    "InteractionRepository",
]
