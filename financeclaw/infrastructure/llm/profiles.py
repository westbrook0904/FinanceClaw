"""模型档案契约：声明模型的调用参数与合规约束，并提供不可变档案目录。

本模块属于 infrastructure 层的 LLM 适配：档案是版本化的配置契约，
orchestration 与 bootstrap 按引用（profile_id + 版本）解析档案，
确保模型切换可追溯、可回滚。
"""

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from financeclaw.kernel import DataClassification


class ModelProfileRef(BaseModel):
    """模型档案引用：以 profile_id 与语义化版本唯一定位一个档案。

    使用场景：工作流定义、降级链等场景中引用模型而不内嵌参数，
    保证配置变更受版本管理约束。

    Attributes:
        profile_id: 档案标识，如 ``default``。
        version: 档案版本，须符合语义化版本（如 ``1.0.0``）。

    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class ModelProfile(BaseModel):
    """模型档案：单个模型的完整调用参数与数据分级、区域等合规约束。

    使用场景：由 bootstrap.py 组合根按配置构造并注册进目录；orchestration
    层据此初始化模型，降级链校验也依赖档案中的能力声明。

    Attributes:
        profile_id: 档案标识。
        version: 档案版本，须符合语义化版本。
        model: 模型标识，格式为 ``provider:model``（如 ``openai:deepseek-v4-pro``）。
        temperature: 采样温度 [0, 2]，金融场景默认 0 以保证确定性。
        timeout_seconds: 单次调用超时（秒），取值范围 (0, 600]。
        max_tokens: 单次生成的最大 token 数，下限 64。
        fallback_profiles: 降级档案引用序列，按顺序尝试。
        allowed_data_classes: 允许处理的数据敏感级别集合，默认放开全部分级。
        allowed_regions: 允许部署/处理数据的区域集合，默认仅 ``global``。
        supports_tool_calling: 是否支持工具调用。
        supports_structured_output: 是否支持结构化输出。

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
        """档案在目录中的唯一键：(profile_id, version)。"""
        return self.profile_id, self.version


class ModelProfileCatalog(Mapping[tuple[str, str], ModelProfile]):
    """不可变模型档案目录：按 (profile_id, version) 索引并解析档案。

    使用场景：由 bootstrap.py 汇集全部档案后构造，注入 ``ModelFactory``
    与编排层；目录只读，运行期不允许增改档案。
    """

    def __init__(self, profiles: Iterable[ModelProfile]) -> None:
        """构建目录并拒绝重复的档案键。

        Args:
            profiles: 待注册的模型档案集合。

        Raises:
            ValueError: 存在 (profile_id, version) 重复的档案。

        """
        entries: dict[tuple[str, str], ModelProfile] = {}
        # 逐个注册，重复键视为配置错误立即失败。
        for profile in profiles:
            if profile.key in entries:
                raise ValueError(f"duplicate model profile: {profile.profile_id}@{profile.version}")
            entries[profile.key] = profile
        # 用 MappingProxyType 包装，保证目录不可变。
        self._entries = MappingProxyType(entries)

    def __getitem__(self, key: tuple[str, str]) -> ModelProfile:
        """按 (profile_id, version) 取档案，键不存在时抛 KeyError。"""
        return self._entries[key]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        """迭代目录中的全部档案键。"""
        return iter(self._entries)

    def __len__(self) -> int:
        """返回目录中的档案数量。"""
        return len(self._entries)

    def resolve(self, ref: ModelProfileRef) -> ModelProfile:
        """把档案引用解析为具体档案。

        Args:
            ref: 模型档案引用。

        Returns:
            对应的模型档案。

        Raises:
            LookupError: 引用的档案在目录中不存在。

        """
        try:
            return self._entries[(ref.profile_id, ref.version)]
        except KeyError as exc:
            raise LookupError(f"unknown model profile: {ref.profile_id}@{ref.version}") from exc
