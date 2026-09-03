"""模型工厂：按模型档案构建 LangChain 聊天模型实例并校验降级链合规性。

本模块属于 infrastructure 层的 LLM 适配，经 OpenAI 兼容协议初始化模型；
SDK 层重试统一关闭（``max_retries=0``），由上层按预算控制重试策略。
"""

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

from .profiles import ModelProfile, ModelProfileCatalog, ModelProfileRef


class ModelFactory:
    """模型工厂：把不可变的模型档案解析为可调用的 LangChain 模型实例。

    使用场景：由 bootstrap.py 组合根携带 API 密钥与 OpenAI 兼容基址构造；
    orchestration 层通过 ``create()`` 获取主模型，通过 ``fallback_models()``
    获取已校验合规的降级模型序列。

    Attributes:
        catalog: 不可变模型档案目录，负责按 (profile_id, version) 解析档案。
        _api_key: Provider API 密钥；为 None 交由环境变量等默认机制提供。
        _base_url: OpenAI 兼容 API 基址；为 None 时使用 SDK 默认端点。

    """

    def __init__(
        self,
        catalog: ModelProfileCatalog,
        *,
        api_key: SecretStr | None,
        base_url: str | None,
    ) -> None:
        """保存目录与连接参数，实际构建延迟到 ``create()`` 调用时。

        Args:
            catalog: 模型档案目录。
            api_key: Provider API 密钥。
            base_url: OpenAI 兼容 API 基址。

        """
        self.catalog = catalog
        self._api_key = api_key
        self._base_url = base_url

    def create(self, ref: ModelProfileRef) -> BaseChatModel:
        """按档案引用初始化一个聊天模型实例。

        Args:
            ref: 模型档案引用（profile_id 与语义化版本）。

        Returns:
            配置好温度、超时、token 上限与连接参数的模型实例。

        Raises:
            LookupError: 档案引用在目录中不存在。
            TypeError: 初始化结果不是 LangChain 聊天模型。

        """
        # 1. 解析档案得到调用参数。
        profile = self.catalog.resolve(ref)
        kwargs: dict[str, object] = {
            "temperature": profile.temperature,
            "timeout": profile.timeout_seconds,
            "max_tokens": profile.max_tokens,
            # 重试由上层按预算统一控制，SDK 内建重试关闭。
            "max_retries": 0,
        }
        # 2. 注入密钥与 OpenAI 兼容基址（如 DeepSeek）。
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key.get_secret_value()
        if self._base_url is not None:
            kwargs["base_url"] = self._base_url
        # 3. 初始化模型并校验类型，防止配置错误静默流入上层。
        model = init_chat_model(profile.model, **kwargs)
        if not isinstance(model, BaseChatModel):
            raise TypeError("model profile did not create a BaseChatModel")
        return model

    def fallback_models(self, profile: ModelProfile) -> tuple[BaseChatModel, ...]:
        """为主模型构建降级模型序列，逐一校验降级模型的合规约束。

        使用场景：主模型不可用时按序切换；每个降级模型的数据分级、区域
        与能力约束必须覆盖主模型，避免降级引入合规风险。

        Args:
            profile: 主模型档案，携带降级引用列表。

        Returns:
            按声明顺序构建的降级模型元组。

        Raises:
            ValueError: 任一降级模型不满足主模型的数据分级、区域或能力要求。

        """
        models: list[BaseChatModel] = []
        # 逐个解析、校验并构建降级模型。
        for ref in profile.fallback_profiles:
            fallback = self.catalog.resolve(ref)
            self._validate_fallback(profile, fallback)
            models.append(self.create(ref))
        return tuple(models)

    @staticmethod
    def _validate_fallback(primary: ModelProfile, fallback: ModelProfile) -> None:
        """校验降级模型的合规约束不弱于主模型。

        Args:
            primary: 主模型档案。
            fallback: 待校验的降级模型档案。

        Raises:
            ValueError: 降级模型允许的数据分级更少、区域受限，或缺少
                主模型所依赖的工具调用/结构化输出能力。

        """
        # 降级模型不得收紧数据分级白名单。
        if not primary.allowed_data_classes.issubset(fallback.allowed_data_classes):
            raise ValueError("fallback model permits fewer data classifications than primary")
        # 降级模型必须满足主模型的区域约束。
        if not primary.allowed_regions.issubset(fallback.allowed_regions):
            raise ValueError("fallback model does not satisfy primary region constraints")
        # 能力约束：主模型具备的能力降级模型必须同样具备。
        if primary.supports_tool_calling and not fallback.supports_tool_calling:
            raise ValueError("fallback model must support tool calling")
        if primary.supports_structured_output and not fallback.supports_structured_output:
            raise ValueError("fallback model must support structured output")
