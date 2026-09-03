"""定义可版本化的模型配置及其只读目录。"""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from financeclaw.kernel import DataClassification


class ModelProfileRef(BaseModel):
    """定义模型配置Ref。

    适用场景：
        用于在接口、领域与持久化边界之间传递经过校验的结构化数据。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        profile_id: 版本化配置的稳定标识。
        version: 语义版本，用于固定运行行为并支持审计复现。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class ModelProfile(BaseModel):
    """定义固定供应商模型参数与数据边界的版本配置。

    适用场景：
        用于以版本化配置固定运行行为，确保审计与结果可复现。

    属性：
        model_config: Pydantic 校验策略，禁止未知字段并在需要时冻结实例。
        profile_id: 版本化配置的稳定标识。
        version: 语义版本，用于固定运行行为并支持审计复现。
        model: 供应商模型名称。
        temperature: 模型采样温度；较低值用于提高可复现性。
        timeout_seconds: 该操作允许的最长时间（秒）。
        max_tokens: 该步骤可用或实际使用的 token 数量。
        fallback_profiles: 主模型失败时按顺序尝试的备用模型配置引用。
        allowed_data_classes: 该配置允许发送或处理的数据分类集合。
        allowed_regions: 模型请求允许落地或处理数据的区域集合。
        supports_tool_calling: 供应方或运行目标是否支持该能力。
        supports_structured_output: 供应方或运行目标是否支持该能力。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    model: str
    temperature: float = Field(default=0, ge=0, le=2)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_tokens: int = Field(default=4096, ge=64)
    fallback_profiles: tuple[ModelProfileRef, ...] = ()
    allowed_data_classes: frozenset[DataClassification] = Field(
        default_factory=lambda: frozenset(DataClassification)
    )
    allowed_regions: frozenset[str] = Field(default_factory=lambda: frozenset({"global"}))
    supports_tool_calling: bool = True
    supports_structured_output: bool = True

    @property
    def key(self) -> tuple[str, str]:
        """返回由稳定标识与版本组成的目录复合键。"""
        return self.profile_id, self.version


class ModelProfileCatalog(Mapping[tuple[str, str], ModelProfile]):
    """保存不可变模型配置，以“配置 ID + 版本”稳定解析。

    适用场景：
        用于运行时必须按显式版本复现配置，或选择最新兼容版本的场景。

    属性：
        _entries: 按复合键索引的只读目录内容。
    """

    def __init__(self, profiles: Iterable[ModelProfile]) -> None:
        """注入并保存模型配置Catalog所需的协作对象，同时校验构造期不变量。"""
        entries: dict[tuple[str, str], ModelProfile] = {}
        for profile in profiles:
            if profile.key in entries:
                raise ValueError(f"duplicate model profile: {profile.profile_id}@{profile.version}")
            entries[profile.key] = profile
        self._entries = MappingProxyType(entries)

    def __getitem__(self, key: tuple[str, str]) -> ModelProfile:
        """按键读取目录项，保持 Mapping 接口语义。"""
        return self._entries[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        """按目录内部的稳定顺序迭代所有键。"""
        return iter(self._entries)

    def __len__(self) -> int:
        """返回目录当前登记的条目数量。"""
        return len(self._entries)

    def resolve(self, ref: ModelProfileRef) -> ModelProfile:
        """解析并校验模型配置Catalog，返回固定版本的运行对象。"""
        try:
            return self._entries[(ref.profile_id, ref.version)]
        except KeyError as exc:
            raise LookupError(f"unknown model profile: {ref.profile_id}@{ref.version}") from exc
