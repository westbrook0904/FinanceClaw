"""本地插件的发现、生命周期管理和注册。

本包将插件来源与 Capability Registry 分离，
便于未来增加远程或其他协议的 Provider。
"""

from .loader import LoadedPlugin, LocalPluginLoader, PluginState
from .provider import LocalPluginProvider

__all__ = [
    "LoadedPlugin",
    "LocalPluginLoader",
    "LocalPluginProvider",
    "PluginState",
]
