"""模型容量与近似计数；不选择历史，也不修改用户消息。"""

import json
import os
import tempfile
from collections.abc import Sequence
from hashlib import sha1
from pathlib import Path
from typing import Any

import tiktoken
from langchain_core.messages import BaseMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from financeclaw.kernel.models import ModelProfile


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
        """应用独立输入cap仅扣安全余量；输出预留由冻结模型总窗口单独扣除。"""
        return self.model_input_limit - self.safety_margin

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

    def __init__(self, estimator_id: str = "cl100k_base-v1") -> None:
        """初始化计数器：tiktoken 可用时加载 cl100k_base 编码，否则留空退化。"""
        if estimator_id not in {"cl100k_base-v1", "utf8-bytes-v1"}:
            raise ValueError(f"unsupported token estimator: {estimator_id}")
        self._encoding = None
        self.estimator_id = "utf8-bytes-v1"
        if estimator_id == "cl100k_base-v1" and _tiktoken_cache_available():
            try:
                self._encoding = tiktoken.get_encoding("cl100k_base")
                self.estimator_id = estimator_id
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


def response_schema(value: Any) -> Any:
    """统一提取结构化输出 Schema，模型前准备与最终检查使用同一表达。"""
    schema = getattr(value, "schema", value)
    if schema is None or isinstance(schema, dict):
        return schema
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    spec = getattr(value, "schema_spec", None)
    return getattr(spec, "json_schema", str(schema))


def request_payload(messages, *, tools=(), output_schema=None, model_settings=None) -> dict:
    """计算完整模型输入的规范载荷，不把本地模型名当作额外用户上下文。"""
    serialized = []
    for message in messages:
        value = message.model_dump(mode="json")
        # Omission diagnostics feed the local Manifest, never the provider prompt.
        # Counting them would let removing optional memory itself cause overflow.
        value.get("additional_kwargs", {}).pop("financeclaw_memory_omissions", None)
        serialized.append(value)
    return {
        "messages": serialized,
        "tools": [convert_to_openai_tool(tool) for tool in tools],
        "response_format": response_schema(output_schema),
        "settings": model_settings or {},
    }


class CommonTokenCounter:
    """主模型与fallback估算器不同时，片段选择同样采用共同最保守计数。"""

    def __init__(self, counters: Sequence[TokenCounter]) -> None:
        """只保存冻结计数器，绝不根据供应商响应改变当前任务容量。"""
        self.counters = tuple(counters)
        self.estimator_id = "+".join(sorted({counter.estimator_id for counter in counters}))

    def text(self, value: str) -> int:
        """返回每个降级链估算器对同一文本的最大估计。"""
        return max(counter.text(value) for counter in self.counters)

    def message(self, message: BaseMessage) -> int:
        """工具片段和最近上下文边界也不能只使用主模型的乐观计数。"""
        return max(counter.message(message) for counter in self.counters)

    def truncate(self, value: str, max_tokens: int) -> str:
        """只为可选检索正文寻找所有估算器都满足的原始字符串前缀。"""
        low, high = 0, len(value)
        while low < high:
            midpoint = (low + high + 1) // 2
            if self.text(value[:midpoint]) <= max_tokens:
                low = midpoint
            else:
                high = midpoint - 1
        return value[:low]


class ContextBudgetPlanner:
    """以冻结主模型及 fallback 的共同窗口准备输入，并逐请求复验容量。"""

    def __init__(
        self,
        profile: ModelProfile,
        application_input_cap: int,
        *,
        safety_margin: int = 0,
        output_reserve: int | None = None,
        fallback_profiles: Sequence[ModelProfile] = (),
    ) -> None:
        """输出仅从总窗口扣除；Provider 独立输入 cap 不重复扣输出。"""
        self.profiles = (profile, *fallback_profiles)
        self.application_input_cap = application_input_cap
        self.safety_margin = safety_margin
        self.output_reserve = output_reserve
        self.counters = tuple(TokenCounter(item.token_estimator) for item in self.profiles)
        self.counter = CommonTokenCounter(self.counters)
        self.input_limit = min(self.limit_for(item) for item in self.profiles)
        if self.input_limit < 256:
            raise ValueError("model context reserves leave fewer than 256 input tokens")

    def limit_for(self, profile: ModelProfile) -> int:
        """计算指定冻结模型的输入上限，不能通过 request 设置扩大它。"""
        reserved = max(profile.max_tokens, self.output_reserve or 0)
        return (
            min(
                self.application_input_cap,
                profile.max_input_tokens or profile.context_window_tokens,
                profile.context_window_tokens - reserved,
            )
            - self.safety_margin
        )

    @property
    def estimator_id(self) -> str:
        """返回实际使用的估算器版本，离线降级也明确记录。"""
        return "+".join(sorted({counter.estimator_id for counter in self.counters}))

    def estimate(self, messages, *, tools=(), output_schema=None, model_settings=None) -> int:
        """估算完整消息、系统内容、工具及输出 Schema 的共同保守 token 数。"""
        canonical = json.dumps(
            request_payload(
                messages, tools=tools, output_schema=output_schema, model_settings=model_settings
            ),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return max(counter.text(canonical) for counter in self.counters)

    def check(self, messages, *, tools=(), output_schema=None, model_settings=None) -> int:
        """返回估算输入数；超过容量则失败，绝不截断必保用户输入。"""
        tokens = self.estimate(
            messages, tools=tools, output_schema=output_schema, model_settings=model_settings
        )
        if tokens > self.input_limit:
            raise ValueError(
                f"mandatory model context exceeds input budget: {tokens} > {self.input_limit}"
            )
        return tokens

    @classmethod
    def from_model(
        cls, model, budget: ContextBudget, *, reserve_application_output: bool = True
    ) -> "ContextBudgetPlanner":
        """生产使用模型携带的冻结档案；测试替身使用显式应用预算。"""
        frozen = (getattr(model, "metadata", None) or {}).get("financeclaw_model_profile")
        profile = (
            ModelProfile.model_validate(frozen)
            if frozen and "context_window_tokens" in frozen
            else ModelProfile(
                profile_id="explicit-test-model",
                version="1.0.0",
                model=type(model).__name__,
                context_window_tokens=budget.model_input_limit,
                max_tokens=budget.reserved_output_tokens,
            )
        )
        return cls(
            profile,
            budget.model_input_limit,
            safety_margin=budget.safety_margin,
            output_reserve=budget.reserved_output_tokens if reserve_application_output else None,
        )
