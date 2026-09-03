"""依据受治理的模型配置创建主模型与回退模型实例。"""

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

from .profiles import ModelProfile, ModelProfileCatalog, ModelProfileRef


class ModelFactory:
    """按照模型配置创建供应商客户端，并构造配置声明的回退链。

    适用场景：
        用于集中表达该职责，避免调用方直接依赖底层实现细节。

    属性：
        catalog: 用于解析固定版本目标的只读目录。
        _api_key: 内部 `api key` 状态或依赖，不属于公开接口。
        _base_url: 内部 `base url` 状态或依赖，不属于公开接口。
    """

    def __init__(
        self,
        catalog: ModelProfileCatalog,
        *,
        api_key: SecretStr | None,
        base_url: str | None,
    ) -> None:
        """注入并保存模型Factory所需的协作对象，同时校验构造期不变量。"""
        self.catalog = catalog
        self._api_key = api_key
        self._base_url = base_url

    def create(self, ref: ModelProfileRef) -> BaseChatModel:
        """解析模型配置，校验供应商凭证，并创建参数受限的聊天模型。"""
        profile = self.catalog.resolve(ref)
        kwargs: dict[str, object] = {
            "temperature": profile.temperature,
            "timeout": profile.timeout_seconds,
            "max_tokens": profile.max_tokens,
            "max_retries": 0,
        }
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key.get_secret_value()
        if self._base_url is not None:
            kwargs["base_url"] = self._base_url
        model = init_chat_model(profile.model, **kwargs)
        if not isinstance(model, BaseChatModel):
            raise TypeError("model profile did not create a BaseChatModel")
        return model

    def fallback_models(self, profile: ModelProfile) -> tuple[BaseChatModel, ...]:
        """按主配置声明的顺序创建回退模型链。"""
        models: list[BaseChatModel] = []
        for ref in profile.fallback_profiles:
            fallback = self.catalog.resolve(ref)
            self._validate_fallback(profile, fallback)
            models.append(self.create(ref))
        return tuple(models)

    @staticmethod
    def _validate_fallback(primary: ModelProfile, fallback: ModelProfile) -> None:
        """校验模型Factory的跨字段不变量并返回自身。"""
        if not primary.allowed_data_classes.issubset(fallback.allowed_data_classes):
            raise ValueError("fallback model permits fewer data classifications than primary")
        if not primary.allowed_regions.issubset(fallback.allowed_regions):
            raise ValueError("fallback model does not satisfy primary region constraints")
        if primary.supports_tool_calling and not fallback.supports_tool_calling:
            raise ValueError("fallback model must support tool calling")
        if primary.supports_structured_output and not fallback.supports_structured_output:
            raise ValueError("fallback model must support structured output")
