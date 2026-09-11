"""模型容量与近似计数；不选择历史，也不修改用户消息。"""

import json
import os
import tempfile
from hashlib import sha1
from pathlib import Path

import tiktoken
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContextBudget(BaseModel):
    """进入模型调用的输入 token 预算配置（不可变）。

    使用场景：bootstrap 阶段依据模型输入上限与各类预留构建，由
    原生上下文策略 用其约束每次装配可用的历史上下文规模。

    Attributes:
        model_config: Pydantic 模型配置；extra="forbid" 拒绝未知字段，
            frozen=True 保证配置不可变。
        model_input_limit: 模型最大输入 token 上限，至少 1024。
        reserved_output_tokens: 为模型输出预留的 token 数，至少 64。
        system_policy_reserve: 为系统提示（策略部分）预留的 token 数，至少 0。
        tool_schema_reserve: 为工具 schema 预留的 token 数，至少 0。
        safety_margin: 额外安全余量，吸收计数误差，至少 0。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_input_limit: int = Field(ge=1_024)
    reserved_output_tokens: int = Field(ge=64)
    system_policy_reserve: int = Field(ge=0)
    tool_schema_reserve: int = Field(ge=0)
    safety_margin: int = Field(ge=0)
    recent_turns: int = Field(default=4, ge=0, le=100)
    summary_trigger_tokens: int = Field(default=64_000, ge=256)
    soft_input_tokens: int = Field(default=96_000, ge=256)
    tool_results_to_keep: int = Field(default=3, ge=0, le=100)

    @property
    def model_request_limit(self) -> int:
        """完整模型输入的上限；输入已含系统和工具，仅扣输出预留及安全余量。"""
        return self.model_input_limit - self.reserved_output_tokens - self.safety_margin

    @property
    def available_input_tokens(self) -> int:
        """计算扣除全部预留后，可用于历史上下文的输入 token 数。

        使用场景：装配前确定预算基数；构造时若该值低于 256 会拒绝创建。

        Returns:
            int: 输入上限减去输出预留、系统预留、工具预留与安全余量后的剩余值。

        """
        return self.model_request_limit - self.system_policy_reserve - self.tool_schema_reserve

    @model_validator(mode="after")
    def validate_available_budget(self) -> "ContextBudget":
        """校验扣除预留后的可用输入预算不低于 256 token。

        使用场景：构造 ContextBudget 时自动执行，避免配置失衡导致上下文无法装配。

        Returns:
            ContextBudget: 校验通过后的实例。

        Raises:
            ValueError: 可用输入 token 少于 256 时抛出。

        """
        if self.available_input_tokens < 256:
            raise ValueError("context reserves leave fewer than 256 input tokens")
        return self


class TokenCounter:
    """token 计数与截断工具：优先用 tiktoken 估算，退化时按 UTF-8 字节估算。

    使用场景：原生上下文策略 用其统计系统提示与消息的 token 占用，
    并在预算不足时按 token 边界生成有界的检索片段。
    """

    def __init__(self) -> None:
        """初始化计数器：tiktoken 可用时加载 cl100k_base 编码，否则留空退化。"""
        self._encoding = None
        if _tiktoken_cache_available():
            try:
                self._encoding = tiktoken.get_encoding("cl100k_base")
            except Exception:
                self._encoding = None

    def text(self, value: str) -> int:
        """统计一段文本的 token 数。

        Args:
            value: 待统计文本。

        Returns:
            int: tiktoken 可用时返回编码估算值，否则返回 UTF-8 字节数估算值。

        """
        if self._encoding is not None:
            return len(self._encoding.encode(value))
        return _estimated_tokens(value)

    def message(self, message: BaseMessage) -> int:
        """统计一条 LangChain 消息序列化后的 token 数。

        Args:
            message: 待统计的消息对象。

        Returns:
            int: 消息 JSON 序列化结果的 token 数，另加 4 个 token 的消息边界开销。

        """
        payload = json.dumps(message.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        return 4 + self.text(payload)

    def truncate(self, value: str, max_tokens: int) -> str:
        """将文本截断到不超过指定 token 数，保留最靠前的内容。

        使用场景：预算不足时生成有界的检索片段；不得用于清空用户输入。

        Args:
            value: 原始文本。
            max_tokens: 允许的最大 token 数；不大于 0 时返回空字符串。

        Returns:
            str: 未超限时返回原文；超限时按 token 边界（或估算二分）截断的前缀。

        """
        if max_tokens <= 0:
            return ""
        if self._encoding is not None:
            tokens = self._encoding.encode(value)
            if len(tokens) <= max_tokens:
                return value
            return self._encoding.decode(tokens[:max_tokens])
        if self.text(value) <= max_tokens:
            return value
        low, high = 0, len(value)
        while low < high:
            midpoint = (low + high + 1) // 2
            if self.text(value[:midpoint]) <= max_tokens:
                low = midpoint
            else:
                high = midpoint - 1
        return value[:low]


def _estimated_tokens(value: str) -> int:
    """退化场景下的 token 估算：直接以 UTF-8 编码字节数作为 token 数。

    Args:
        value: 待估算文本。

    Returns:
        int: 估算 token 数（UTF-8 字节数）。

    """
    return len(value.encode("utf-8"))


def _tiktoken_cache_available() -> bool:
    """检查本地是否已有 cl100k_base 编码缓存，避免初始化时联网下载。

    使用场景：TokenCounter 初始化前探测；离线环境据此退化为字节估算。

    Returns:
        bool: 缓存目录未显式置空且编码缓存文件存在时返回 True。

    """
    cache_root = os.getenv("TIKTOKEN_CACHE_DIR", os.getenv("DATA_GYM_CACHE_DIR"))
    if cache_root == "":
        return False
    root = Path(cache_root) if cache_root else Path(tempfile.gettempdir()) / "data-gym-cache"
    source = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
    return (root / sha1(source.encode()).hexdigest()).is_file()
