"""插件聚合与生命周期接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from .capability import Capability
from .models import PluginManifest


class PluginSPI(ABC):
    """本地插件向 Loader 暴露的阶段一接口。

    ``manifest`` 和 ``capabilities`` 必须无副作用且在插件存活期间保持稳定。
    Loader 只调用一次 ``initialize`` 和 ``shutdown``；实现仍应保证二者幂等，
    以便启动失败时能够安全清理。

    这里刻意不提供通用 ``execute``，调用必须落到具体 Agent 或 Tool 上。
    """

    @abstractmethod
    def manifest(self) -> PluginManifest:
        """返回插件清单。"""

    @abstractmethod
    def capabilities(self) -> Sequence[Capability]:
        """返回此插件提供的 Agent 和 Tool。"""

    @abstractmethod
    async def initialize(self) -> None:
        """初始化插件资源；重复调用不得产生额外副作用。"""

    @abstractmethod
    async def shutdown(self) -> None:
        """释放插件资源；未初始化或重复关闭也应安全。"""
