"""Thin ModelProfile-to-LangChain model adapter."""

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

from .profiles import ModelProfile, ModelProfileCatalog, ModelProfileRef


class ModelFactory:
    def __init__(
        self,
        catalog: ModelProfileCatalog,
        *,
        api_key: SecretStr | None,
        base_url: str | None,
    ) -> None:
        self.catalog = catalog
        self._api_key = api_key
        self._base_url = base_url

    def create(self, ref: ModelProfileRef) -> BaseChatModel:
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
        models: list[BaseChatModel] = []
        for ref in profile.fallback_profiles:
            fallback = self.catalog.resolve(ref)
            self._validate_fallback(profile, fallback)
            models.append(self.create(ref))
        return tuple(models)

    @staticmethod
    def _validate_fallback(primary: ModelProfile, fallback: ModelProfile) -> None:
        if not primary.allowed_data_classes.issubset(fallback.allowed_data_classes):
            raise ValueError("fallback model permits fewer data classifications than primary")
        if not primary.allowed_regions.issubset(fallback.allowed_regions):
            raise ValueError("fallback model does not satisfy primary region constraints")
        if primary.supports_tool_calling and not fallback.supports_tool_calling:
            raise ValueError("fallback model must support tool calling")
        if primary.supports_structured_output and not fallback.supports_structured_output:
            raise ValueError("fallback model must support structured output")
