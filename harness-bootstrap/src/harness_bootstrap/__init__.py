"""FinanceClaw 的依赖组装与应用生命周期入口。

本包是阶段一唯一的 Composition Root：负责连接具体基础设施实现，但不包含业务逻辑。
"""

from .application import (
    BootstrapState,
    BootstrapStateError,
    HarnessApplication,
    HarnessComponents,
)
from .factory import build_harness

__all__ = [
    "BootstrapState",
    "BootstrapStateError",
    "HarnessApplication",
    "HarnessComponents",
    "build_harness",
]
