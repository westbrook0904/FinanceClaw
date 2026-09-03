"""LLM 接入适配：模型档案契约与模型工厂。

本包属于 infrastructure 层，经 OpenAI 兼容协议接入 LLM Provider（如
DeepSeek），由 bootstrap.py 组合根按配置装配后供 orchestration 使用。
"""

from .factory import ModelFactory
from .profiles import ModelProfile, ModelProfileCatalog, ModelProfileRef

__all__ = ["ModelFactory", "ModelProfile", "ModelProfileCatalog", "ModelProfileRef"]
