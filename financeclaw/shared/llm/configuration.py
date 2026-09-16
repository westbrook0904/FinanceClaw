"""启动时加载供应商、模型别名与 Agent 绑定；声明中不保存运行凭据。"""

import tomllib
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, create_model, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from financeclaw.kernel.models import ModelProfile, ModelProfileCatalog, ModelProfileRef

Alias = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")]


class ProviderDeclaration(BaseModel):
    """OpenAI 兼容服务地址和密钥环境变量名；实际密钥在执行端读取。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    base_url: str
    api_key_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")

    @model_validator(mode="after")
    def validate_url(self):
        """地址必须是纯 HTTP(S) 端点，不能把凭据混入发布声明。"""
        parts = urlsplit(self.base_url)
        if (
            parts.scheme not in {"https", "http"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("provider base_url must be an HTTP(S) endpoint without credentials")
        return self

    def secret(self, *, env_file=None) -> SecretStr:
        """沿用 pydantic-settings 的环境变量优先级，支持本地 dotenv 与部署注入。"""
        credentials = create_model(
            "ProviderCredentials",
            __base__=BaseSettings,
            __config__=SettingsConfigDict(extra="ignore", hide_input_in_errors=True),
            key=(SecretStr, Field(validation_alias=self.api_key_env)),
        )(_env_file=env_file)
        if not credentials.key.get_secret_value().strip():
            raise ValueError(f"empty provider credential: {self.api_key_env}")
        return credentials.key


class ModelDeclaration(BaseModel):
    """一个可复用别名的调用参数与显式容量；降级候选也只引用别名。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    provider: Alias
    model: str = Field(pattern=r"^openai:[^\s]+$")
    context_window_tokens: int = Field(ge=1024)
    max_input_tokens: int | None = None
    max_tokens: int = 4096
    enable_thinking: bool | None = None
    temperature: float = 0
    timeout_seconds: float = 300
    token_estimator: str = "cl100k_base-v1"
    fallbacks: tuple[Alias, ...] = ()


class DefaultBinding(BaseModel):
    """所有未覆盖的 Agent 与后台任务共享的默认模型别名。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    model: Alias


class TaskBindings(BaseModel):
    """后台用途的可选覆盖；省略字段时使用 defaults.model。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    summary: Alias | None = None
    memory_extraction: Alias | None = None
    memory_consolidation: Alias | None = None


class ModelConfiguration(BaseModel):
    """所有聊天模型的唯一配置来源；默认绑定、Agent 和后台任务统一引用别名。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    providers: dict[Alias, ProviderDeclaration]
    models: dict[Alias, ModelDeclaration]
    defaults: DefaultBinding
    agents: dict[Alias, Alias] = Field(default_factory=dict)
    tasks: TaskBindings = Field(default_factory=TaskBindings)

    @classmethod
    def from_file(cls, path: str):
        """读取 TOML；文件、字段或引用错误直接阻止发布，不回退到其他模型。"""
        with Path(path).open("rb") as stream:
            return cls.model_validate(tomllib.load(stream))

    def fallback_aliases(self, alias: str) -> tuple[str, ...]:
        """按声明顺序展开降级链，拒绝循环并对重复候选去重。"""
        ordered: list[str] = []

        def visit(name, path):
            """沿声明顺序遍历候选，并在当前路径上检测循环。"""
            if name not in self.models:
                raise ValueError(f"unknown model alias: {name}")
            if name in path:
                raise ValueError(f"cyclic model fallbacks: {' -> '.join((*path, name))}")
            for child in self.models[name].fallbacks:
                if child not in ordered:
                    ordered.append(child)
                visit(child, (*path, name))

        visit(alias, ())
        return tuple(ordered)

    def ref(self, agent_id: str) -> ModelProfileRef:
        """Agent 代码仅使用稳定标识；模型选择来自配置中的别名绑定。"""
        return ModelProfileRef(
            profile_id=self.agents.get(agent_id, self.defaults.model), version="1.0.0"
        )

    def task_ref(self, purpose: str) -> ModelProfileRef:
        """已知后台任务可覆盖默认模型；用途拼写错误立即失败。"""
        if purpose not in TaskBindings.model_fields:
            raise ValueError(f"unknown model task: {purpose}")
        return ModelProfileRef(
            profile_id=getattr(self.tasks, purpose) or self.defaults.model, version="1.0.0"
        )

    def agent_refs(self, *, ziwei_enabled: bool) -> tuple[ModelProfileRef, ...]:
        """图执行端仅初始化当前可达 Agent 与摘要的连接。"""
        return (
            self.ref("finance_agent"),
            self.ref("market_research_agent"),
            *((self.ref("ziwei_doushu_agent"),) if ziwei_enabled else ()),
            self.task_ref("summary"),
        )

    def memory_refs(self) -> tuple[ModelProfileRef, ...]:
        """记忆执行端只需要提取和整理的连接，不读取其他供应商凭据。"""
        return tuple(
            self.task_ref(purpose) for purpose in ("memory_extraction", "memory_consolidation")
        )

    def profiles(self) -> tuple[ModelProfile, ...]:
        """把用户声明编译成现有 ModelProfile，不引入第二套模型运行时。"""
        return tuple(
            ModelProfile(
                profile_id=alias,
                version="1.0.0",
                connection_id=item.provider,
                **item.model_dump(exclude={"provider", "fallbacks"}),
                fallback_profiles=tuple(
                    ModelProfileRef(profile_id=name, version="1.0.0")
                    for name in self.fallback_aliases(alias)
                ),
            )
            for alias, item in self.models.items()
        )

    @model_validator(mode="after")
    def validate_references(self):
        """启动前检查供应商、模型、降级和绑定的完整性，以及模型参数有效性。"""
        for item in self.models.values():
            if item.provider not in self.providers:
                raise ValueError(f"unknown provider alias: {item.provider}")
        for alias in (
            self.defaults.model,
            *self.agents.values(),
            *(value for value in self.tasks.model_dump().values() if value is not None),
        ):
            if alias not in self.models:
                raise ValueError(f"unknown model alias: {alias}")
        self.profiles()
        return self

    def active_providers(
        self, refs=None, *, include_fallbacks=True
    ) -> dict[str, ProviderDeclaration]:
        """只返回指定用途及降级链实际引用的供应商，候选库存不要求填写密钥。"""
        if refs is None:
            refs = (*self.agent_refs(ziwei_enabled=True), *self.memory_refs())
        catalog = ModelProfileCatalog(self.profiles())
        identifiers = {
            profile.connection_id
            for ref in refs
            for profile in (
                catalog.dependencies(ref) if include_fallbacks else (catalog.resolve(ref),)
            )
        }
        return {identifier: self.providers[identifier] for identifier in sorted(identifiers)}

    def release(self, ref: ModelProfileRef):
        """冻结单个调用的模型依赖和端点；不包含密钥或无关模型的声明。"""
        profiles = ModelProfileCatalog(self.profiles()).dependencies(ref)
        return profiles, {
            item.connection_id: self.providers[item.connection_id].base_url for item in profiles
        }
